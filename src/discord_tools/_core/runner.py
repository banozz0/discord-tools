"""The runner: the one long-running process per tool, and everything that makes it operable.

Spec: sections 10.1 and 10.4 to 10.6. `watch run` is one process per tool
and per machine, holding an exclusive lock, receiving events through the
`EventSource` Protocol, evaluating them with `rules.Engine`, delivering the
alert intents the engine hands back, and firing the local schedules. It
knows nothing about any other runner; an alert that must reach another
platform goes to a configured command (section 10.4). This module never
imports an SDK and never names a platform: the tool's watch card wires its
event source, its sender and its paths in.

What is solved here once, with fixtures that simulate it:

- **The lock.** `runner.lock` under the tool's directory, held with an
  exclusive `fcntl` lock and carrying the holder's pid and start time. A
  second holder exits `RUNNER_LOCKED` naming the first. The OS releases the
  lock when the process dies, so there is no stale lock to clean. `fcntl`
  is Unix-only: where it is absent, `watch run` is `PLATFORM_UNSUPPORTED`
  and every one-shot command still works.
- **Restart recovery.** One cursor per scope in `runner_state`
  (`cursor:<rid>`), written after the event it names has been evaluated.
  On start the runner replays every scope from its cursor through the
  engine with dedup on, so an event evaluated before the crash is a
  duplicate and an event the crash swallowed fires once.
- **Clock jumps.** Schedules are planned from a monotonic baseline recorded
  with the wall time at start. Each tick compares the wall clock with what
  the monotonic clock says it should be: a backward jump over
  `BACKWARD_JUMP_S` re-plans from a new baseline, and a forward jump fires
  each missed schedule once with `late` set, never once per missed
  occurrence. A restart is a forward jump of however long the runner was
  down. `SimulatedClock` in the tests drives both.
- **Rate limits.** A delivery that raises `RateLimited` is waited for
  through the `wait` callback and retried once; every wait is reported in
  `status()`.
- **Delivery.** `PlatformDelivery` sends through the tool's own
  `MessageSender` under `yes_allowlist` and refuses a rid on another
  platform (that is what a command destination is for);
  `CommandDelivery` runs the configured argv with the alert on stdin and
  reads the status from a `cli-tools/envelope/1` object when the command
  prints one. `Delivery` routes by destination kind. A command absent from
  PATH was already `COMMAND_MISSING` at rule load.
- **The two guarantees.** A schedule the runner holds is reported
  `runner-held: fires only while watch run is up on this machine` in every
  listing; a platform's own scheduling is `server-held`. `GUARANTEES` is
  the closed pair.

The runner neither installs nor prints a service unit: it runs in the
foreground, under a multiplexer, or under a unit the user writes. The
README says how.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - not a Unix build; run() refuses by code
    fcntl = None  # type: ignore[assignment]

from .archive import Archive
from .contract import SCHEMA as ENVELOPE_SCHEMA, STATUSES, CodedError, utc_now
from .identity import Identity
from .paths import ToolPaths, open_private
from .review import ReviewQueue
from .rid import RidError, parse as parse_rid
from .rules import AlertDelivery, AlertIntent, Engine, Evaluation, Event, RuleSet, SyncRequest

# Section 17: `runner_state` carries its own version key; a runner refuses a
# newer state than its code.
RUNNER_STATE_VERSION = 1
VERSION_KEY = "runner_state_version"
BASELINE_KEY = "baseline"
CURSOR_KEY = "cursor:"
SCHEDULE_KEY = "schedule:"
# Section 10.6: the closed pair every schedule listing prints one of.
GUARANTEES = ("server-held", "runner-held")
SERVER_HELD = "server-held"
RUNNER_HELD = "runner-held: fires only while watch run is up on this machine"
# Section 10.5: a backward wall jump larger than this re-plans; a fire this
# far past its wall time is `late`.
BACKWARD_JUMP_S = 60
LATE_TOLERANCE_S = 60
STATUS_LOG_LINES = 20
STOP_WAIT_S = 10
COMMAND_TIMEOUT_S = 30
INTERVAL_PATTERN = r"^([1-9][0-9]*)([smhd])$"
DELIVERY_STATUSES = ("ok", "failed", "refused")

_INTERVAL = re.compile(INTERVAL_PATTERN)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class RunnerError(ValueError):
    """An argument that breaks the runner's own contract."""


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- clocks ------------------------------------------------------------------


class Clock:
    """The two clocks the planner reads: wall time for what the user asked, monotonic for what elapsed."""

    def __init__(self, wall: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.wall = wall
        self.monotonic = monotonic


class SimulatedClock(Clock):
    """The fixture of section 10.5: a clock whose wall time can jump while the monotonic one only advances."""

    def __init__(self, wall: float = 1_800_000_000.0, monotonic: float = 1000.0) -> None:
        self._wall = wall
        self._monotonic = monotonic
        super().__init__(lambda: self._wall, lambda: self._monotonic)

    def advance(self, seconds: float) -> None:
        """Time passes: both clocks move together."""
        self._wall += seconds
        self._monotonic += seconds

    def jump(self, seconds: float) -> None:
        """The wall clock is set forward (positive) or back (negative); nothing elapsed."""
        self._wall += seconds

    def restart(self) -> None:
        """A new process: the monotonic clock starts over, the wall clock does not."""
        self._monotonic = 0.0


# -- the lock ----------------------------------------------------------------


@dataclass(frozen=True)
class LockInfo:
    """What the lock file says about its holder, and whether that process is alive."""

    pid: int
    started_at: str
    alive: bool

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "started_at": self.started_at, "alive": self.alive}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock(path: Path) -> LockInfo | None:
    """The holder the lock file names, or None when there is no file or it is empty."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        data = json.loads(text)
        pid = int(data["pid"])
        started_at = str(data["started_at"])
    except (ValueError, KeyError, TypeError):
        return None
    return LockInfo(pid=pid, started_at=started_at, alive=_pid_alive(pid))


class Lock:
    """`runner.lock`: one exclusive holder per tool, pid and start time inside (section 10.5)."""

    def __init__(self, path: Path, *, pid: int | None = None, started_at: str | None = None) -> None:
        self.path = Path(path)
        self.pid = os.getpid() if pid is None else pid
        self.started_at = started_at or utc_now()
        self._handle: Any = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> LockInfo:
        """Take the lock, or RUNNER_LOCKED naming the holder; PLATFORM_UNSUPPORTED without fcntl."""
        if fcntl is None:
            raise CodedError(
                "PLATFORM_UNSUPPORTED",
                "the runner needs an exclusive file lock, which this platform does not provide",
                hint="watch run supports macOS and Linux; every one-shot command still works here",
            )
        if self._handle is not None:
            return LockInfo(self.pid, self.started_at, True)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = open_private(self.path, "a")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            holder = read_lock(self.path)
            who = f"pid {holder.pid} since {holder.started_at}" if holder else "another process"
            raise CodedError(
                "RUNNER_LOCKED",
                f"a runner already holds {self.path.name}: {who}",
                hint="`watch status` shows the holder; `watch stop` asks it to exit",
            ) from None
        # Truncate under the lock, so the file never mixes two holders' records.
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": self.pid, "started_at": self.started_at}))
        handle.flush()
        self._handle = handle
        return LockInfo(self.pid, self.started_at, True)

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.flush()
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "Lock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


# -- the log -----------------------------------------------------------------


class Log:
    """`runner.log`: one JSON line per thing the runner did, 0600, what `watch status` tails."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def write(self, event: str, **fields: Any) -> dict[str, Any]:
        line = {"at": utc_now(), "event": event, **fields}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open_private(self.path, "a") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        return line


def tail(path: Path, count: int = STATUS_LOG_LINES) -> list[dict[str, Any]]:
    """The last `count` log lines as records; a line that is not JSON is kept as `{"raw": ...}`."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-count:] if count > 0 else []:
        try:
            record = json.loads(line)
        except ValueError:
            record = {"raw": line}
        out.append(record if isinstance(record, dict) else {"raw": line})
    return out


# -- runner_state ------------------------------------------------------------


class RunnerState:
    """The `runner_state` rows the runner owns: version, baseline, cursors, schedules.

    Cooldown windows live in the same table under `cooldown:` and belong to
    the engine. Every write here is its own transaction unless the caller
    holds one.
    """

    def __init__(self, archive: Archive, identity: Identity) -> None:
        self.archive = archive
        self.connection = archive.connection
        self.identity = identity

    def get(self, key: str) -> Any:
        row = self.connection.execute("SELECT value FROM runner_state WHERE key = ?", (key,)).fetchone()
        if row is None or row[0] is None:
            return None
        return json.loads(row[0])

    def set(self, key: str, value: Any, *, inside: bool = False) -> None:
        if not inside:
            self.archive._begin()
        try:
            self.connection.execute(
                "INSERT INTO runner_state (key, identity_id, value, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (key) DO UPDATE SET identity_id = excluded.identity_id, value = excluded.value, updated_at = excluded.updated_at",
                (key, self.identity.id, json.dumps(value, ensure_ascii=False), utc_now()),
            )
            if not inside:
                self.archive._commit()
        except Exception:
            if not inside:
                self.archive._rollback()
            raise

    def delete(self, key: str, *, inside: bool = False) -> None:
        if not inside:
            self.archive._begin()
        try:
            self.connection.execute("DELETE FROM runner_state WHERE key = ?", (key,))
            if not inside:
                self.archive._commit()
        except Exception:
            if not inside:
                self.archive._rollback()
            raise

    def items(self, prefix: str) -> list[tuple[str, Any]]:
        rows = self.connection.execute(
            "SELECT key, value FROM runner_state WHERE key LIKE ? ORDER BY key", (prefix + "%",)
        ).fetchall()
        return [(row[0], json.loads(row[1])) for row in rows if row[1] is not None]

    def check_version(self) -> int:
        """Section 17: write the version when absent; refuse a state newer than this code."""
        stored = self.get(VERSION_KEY)
        if stored is None:
            self.set(VERSION_KEY, RUNNER_STATE_VERSION)
            return RUNNER_STATE_VERSION
        if not isinstance(stored, int) or stored > RUNNER_STATE_VERSION:
            raise CodedError(
                "SCHEMA_MIGRATION_REQUIRED",
                f"runner_state is version {stored!r}, newer than this build's {RUNNER_STATE_VERSION}",
                hint="update the tool; a runner never reads state a newer build wrote",
            )
        return stored

    # -- cursors -----------------------------------------------------------

    def cursor(self, rid: str) -> str | None:
        value = self.get(CURSOR_KEY + rid)
        return None if value is None else str(value["cursor"])

    def cursors(self) -> dict[str, str]:
        return {key[len(CURSOR_KEY):]: str(value["cursor"]) for key, value in self.items(CURSOR_KEY)}

    def save_cursor(self, rid: str, cursor: str) -> None:
        self.set(CURSOR_KEY + rid, {"cursor": str(cursor), "at": utc_now()})


# -- schedules ---------------------------------------------------------------


def parse_at(text: str, tz: tzinfo | None = None) -> datetime:
    """ISO 8601; a time without an offset is local time (or `tz`), echoed with the offset applied."""
    if not isinstance(text, str) or not text.strip():
        raise RunnerError("a schedule time is an ISO 8601 date and time")
    try:
        when = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunnerError(f"{text!r} is not an ISO 8601 date and time: {exc}") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz) if tz is not None else when.astimezone()
    return when


def _cron_field(text: str, low: int, high: int, names: Mapping[str, int] | None = None) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        has_step = "/" in part
        if has_step:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise RunnerError(f"cron step {step_text!r} is not a positive number")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        else:
            bounds = part.split("-", 1)
            try:
                numbers = [int((names or {}).get(bound.lower(), bound)) for bound in bounds]
            except ValueError:
                raise RunnerError(f"cron field {part!r} is not a number, a name or a range") from None
            start = numbers[0]
            end = numbers[1] if len(numbers) == 2 else (high if has_step else start)
        if not (low <= start <= high and low <= end <= high and start <= end):
            raise RunnerError(f"cron field {part!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return values


_MONTHS = {name: number for number, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DAYS = {name: number for number, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}


@dataclass(frozen=True)
class Cron:
    """A five-field cron expression: minute, hour, day of month, month, day of week (0 or 7 is Sunday)."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, text: str) -> "Cron":
        fields = text.split()
        if len(fields) != 5:
            raise RunnerError(f"{text!r} is not a cron expression: expected minute hour day month weekday")
        weekdays = {0 if day == 7 else day for day in _cron_field(fields[4], 0, 7, _DAYS)}
        return cls(
            minutes=frozenset(_cron_field(fields[0], 0, 59)),
            hours=frozenset(_cron_field(fields[1], 0, 23)),
            days=frozenset(_cron_field(fields[2], 1, 31)),
            months=frozenset(_cron_field(fields[3], 1, 12, _MONTHS)),
            weekdays=frozenset(weekdays),
            any_day=fields[2] == "*",
            any_weekday=fields[4] == "*",
        )

    def _day_matches(self, day: datetime) -> bool:
        if day.month not in self.months:
            return False
        by_day = day.day in self.days
        by_weekday = (day.weekday() + 1) % 7 in self.weekdays
        # Standard cron: both restricted means either matches.
        if self.any_day:
            return by_weekday
        if self.any_weekday:
            return by_day
        return by_day or by_weekday

    def next_after(self, after: datetime) -> datetime:
        """The first matching minute strictly after `after`, within a year."""
        start = after.replace(second=0, microsecond=0)
        day = start.replace(hour=0, minute=0)
        for offset in range(0, 367):
            current = day + timedelta(days=offset)
            if not self._day_matches(current):
                continue
            for hour in sorted(self.hours):
                for minute in sorted(self.minutes):
                    candidate = current.replace(hour=hour, minute=minute)
                    if candidate > after:
                        return candidate
        raise RunnerError("the cron expression never matches within a year")


def parse_every(text: str) -> tuple[str, Any]:
    """`every` as an interval (`15m`, `2h`, `1d`) or a cron expression; the kind and the parsed value."""
    if not isinstance(text, str) or not text.strip():
        raise RunnerError("a repeat is an interval like 15m or a five-field cron expression")
    match = _INTERVAL.fullmatch(text.strip())
    if match:
        return "interval", int(match.group(1)) * _UNITS[match.group(2)]
    return "cron", Cron.parse(text.strip())


def next_occurrence(every: str, after_wall: float, tz: tzinfo | None = None) -> float:
    """The next wall time `every` names strictly after `after_wall`."""
    kind, value = parse_every(every)
    if kind == "interval":
        return after_wall + value
    after = datetime.fromtimestamp(after_wall, tz or datetime.now().astimezone().tzinfo)
    return value.next_after(after).timestamp()


@dataclass
class Schedule:
    """One runner-held schedule: what `schedule post --at | --every` stores in `runner_state`."""

    id: str
    rid: str
    text: str
    next_wall: float
    at: str | None = None
    every: str | None = None
    created_at: str = field(default_factory=utc_now)
    fires: int = 0
    last_fired_at: str | None = None
    last_late: bool = False

    @property
    def key(self) -> str:
        return SCHEDULE_KEY + self.id

    @property
    def guarantee(self) -> str:
        return RUNNER_HELD

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rid": self.rid,
            "text": self.text,
            "at": self.at,
            "every": self.every,
            "next": _iso(self.next_wall),
            "next_wall": self.next_wall,
            "created_at": self.created_at,
            "fires": self.fires,
            "last_fired_at": self.last_fired_at,
            "last_late": self.last_late,
            "guarantee": self.guarantee,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Schedule":
        return cls(
            id=str(data["id"]),
            rid=str(data["rid"]),
            text=str(data["text"]),
            next_wall=float(data["next_wall"]),
            at=data.get("at"),
            every=data.get("every"),
            created_at=str(data.get("created_at") or utc_now()),
            fires=int(data.get("fires", 0)),
            last_fired_at=data.get("last_fired_at"),
            last_late=bool(data.get("last_late", False)),
        )


@dataclass(frozen=True)
class ScheduleFire:
    """One firing: which schedule, when, whether it was late, and what delivery said."""

    schedule_id: str
    rid: str
    at: str
    late: bool
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"schedule": self.schedule_id, "rid": self.rid, "at": self.at, "late": self.late, "status": self.status, "error": self.error}


class Schedules:
    """The local schedules of section 10.6 over `runner_state`: add, list, cancel."""

    def __init__(self, state: RunnerState, clock: Clock, tz: tzinfo | None = None) -> None:
        self.state = state
        self.clock = clock
        self.tz = tz

    def add(self, rid: str, text: str, *, at: str | None = None, every: str | None = None) -> Schedule:
        """A schedule at `at` (once) or `every` (repeating), its listing carrying the guarantee."""
        try:
            parse_rid(rid)
        except RidError as exc:
            raise RunnerError(str(exc)) from exc
        if not isinstance(text, str) or not text.strip():
            raise RunnerError("a schedule posts a non-empty text")
        if (at is None) == (every is None):
            raise RunnerError("a schedule is --at <time> or --every <repeat>, one of the two")
        now = self.clock.wall()
        if at is not None:
            when = parse_at(at, self.tz)
            next_wall = when.timestamp()
            if next_wall <= now:
                raise RunnerError(f"{at!r} is in the past")
            spelled_at: str | None = when.isoformat()
        else:
            next_wall = next_occurrence(every, now, self.tz)  # type: ignore[arg-type]
            spelled_at = None
        created = utc_now()
        material = "\n".join([rid, text, spelled_at or "", every or "", created, str(now)])
        schedule = Schedule(
            id=hashlib.sha256(material.encode("utf-8")).hexdigest()[:12],
            rid=rid,
            text=text,
            next_wall=next_wall,
            at=spelled_at,
            every=every,
            created_at=created,
        )
        self.state.set(schedule.key, schedule.to_dict())
        return schedule

    def list(self) -> list[Schedule]:
        return [Schedule.from_dict(value) for _key, value in self.state.items(SCHEDULE_KEY)]

    def get(self, schedule_id: str) -> Schedule:
        value = self.state.get(SCHEDULE_KEY + schedule_id)
        if value is None:
            raise RunnerError(f"no schedule {schedule_id!r}")
        return Schedule.from_dict(value)

    def cancel(self, schedule_id: str) -> Schedule:
        schedule = self.get(schedule_id)
        self.state.delete(schedule.key)
        return schedule

    def save(self, schedule: Schedule) -> None:
        self.state.set(schedule.key, schedule.to_dict())


def listing(schedule: Schedule) -> dict[str, Any]:
    """A schedule as every listing prints it: the guarantee is a field, never inferred."""
    return schedule.to_dict()


# -- delivery ----------------------------------------------------------------


class RateLimited(CodedError):
    """A delivery the platform asked to wait for; the runner honours `retry_after_s` through its wait callback."""

    def __init__(self, retry_after_s: float, message: str = "the platform asked to wait", *, platform: str | None = None) -> None:
        super().__init__("RATE_LIMITED", f"{message} ({retry_after_s:g}s)", retryable=True, platform=platform)
        self.retry_after_s = float(retry_after_s)


def _prefix(rid: str) -> str:
    return parse_rid(rid).prefix


class PlatformDelivery:
    """An alert to a rid on this tool's own platform, through `MessageSender.send` under `yes_allowlist`.

    The sender is the tool's own gated send path, so its allowlist is the
    gate for automated alerts too; `allowlist`, when given, refuses earlier
    with the same `NOT_ALLOWLISTED`. A rid on another platform is refused
    `PLATFORM_UNSUPPORTED`: an alert crosses platforms only through a
    command destination (section 10.4), and this tool never sends there.
    """

    def __init__(self, sender: Any, identity: Identity, *, allowlist: Iterable[str] | None = None) -> None:
        self.sender = sender
        self.identity = identity
        self.allowlist = None if allowlist is None else frozenset(allowlist)
        self.own_prefix = _prefix(identity.id)

    def deliver(self, intent: AlertIntent) -> Mapping[str, Any]:
        return self.send(intent.destination.rid or "", intent.text)

    def send(self, rid: str, text: str) -> Mapping[str, Any]:
        """`text` to `rid` on this platform under `yes_allowlist`; the readback record."""
        try:
            prefix = _prefix(rid)
        except RidError as exc:
            raise CodedError("PLATFORM_UNSUPPORTED", f"destination {rid!r}: {exc}") from exc
        same_platform = prefix == self.own_prefix
        if not same_platform:
            raise CodedError(
                "PLATFORM_UNSUPPORTED",
                f"{rid} is not on this tool's platform",
                hint="an alert reaches another platform through a command destination",
            )
        if self.allowlist is not None and rid not in self.allowlist:
            raise CodedError(
                "NOT_ALLOWLISTED",
                f"{rid} is not in the send allowlist",
                hint="add it to the allowlist, or point the rule at a command destination",
            )
        record = self.sender.send(rid, text, approval="yes_allowlist")
        return {"status": "ok", "rid": rid, "readback": dict(record) if isinstance(record, Mapping) else record}


class CommandDelivery:
    """An alert to a configured argv: the text on stdin, the status from an envelope when one is printed.

    `run` is `subprocess.run`, injectable. The command's stdout is read as a
    `cli-tools/envelope/1` object when it parses as one, and its `status`
    is the delivery's; otherwise exit 0 is `ok` and anything else `failed`.
    A command that has gone missing since rule load is `COMMAND_MISSING`.
    """

    def __init__(self, *, run: Callable[..., Any] = subprocess.run, timeout_s: float = COMMAND_TIMEOUT_S, env: Mapping[str, str] | None = None) -> None:
        self.run = run
        self.timeout_s = timeout_s
        self.env = None if env is None else dict(env)

    def deliver(self, intent: AlertIntent) -> Mapping[str, Any]:
        argv = list(intent.destination.argv)
        if not argv:
            raise CodedError("COMMAND_MISSING", "a command destination names no argv")
        try:
            completed = self.run(
                argv,
                input=intent.text,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=self.env,
            )
        except FileNotFoundError:
            raise CodedError("COMMAND_MISSING", f"alert command {argv[0]!r} is not on PATH", hint="it was there at rule load; `watch reload` re-checks") from None
        except subprocess.TimeoutExpired:
            return {"status": "failed", "returncode": None, "error": f"timed out after {self.timeout_s:g}s"}
        stdout = str(getattr(completed, "stdout", "") or "")
        returncode = int(getattr(completed, "returncode", 0) or 0)
        envelope = _envelope_in(stdout)
        if envelope is not None:
            status = envelope.get("status")
            error = envelope.get("error") or None
            return {"status": status, "returncode": returncode, "envelope": True, "error": error.get("code") if isinstance(error, Mapping) else error}
        return {"status": "ok" if returncode == 0 else "failed", "returncode": returncode, "envelope": False, "error": None if returncode == 0 else stdout.strip()[-200:] or f"exit {returncode}"}


def _envelope_in(stdout: str) -> Mapping[str, Any] | None:
    """The `cli-tools/envelope/1` object `stdout` holds, when it holds one."""
    text = stdout.strip()
    if not text:
        return None
    candidates = [text, *(line for line in text.splitlines() if line.strip())]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, Mapping) and data.get("schema") == ENVELOPE_SCHEMA and data.get("status") in STATUSES:
            return data
    return None


class Delivery:
    """The `AlertDelivery` the runner holds: routes each intent to the delivery of its destination kind."""

    def __init__(self, *, platform: PlatformDelivery | None = None, command: CommandDelivery | None = None) -> None:
        self.to_platform = platform
        self.to_command = command or CommandDelivery()

    def deliver(self, intent: AlertIntent) -> Mapping[str, Any]:
        if intent.destination.kind == "command":
            return self.to_command.deliver(intent)
        if self.to_platform is None:
            raise CodedError("PLATFORM_UNSUPPORTED", "this runner has no sender for platform destinations", hint="the tool wires its own send path in")
        return self.to_platform.deliver(intent)


# -- the runner --------------------------------------------------------------


@dataclass(frozen=True)
class Baseline:
    """Section 10.5: the monotonic reading recorded with the wall time it was taken at."""

    wall: float
    monotonic: float
    recorded_at: str

    def expected_wall(self, monotonic_now: float) -> float:
        return self.wall + (monotonic_now - self.monotonic)

    def planned_monotonic(self, wall_target: float) -> float:
        return self.monotonic + (wall_target - self.wall)

    def to_dict(self) -> dict[str, Any]:
        return {"wall": self.wall, "monotonic": self.monotonic, "recorded_at": self.recorded_at}


@dataclass(frozen=True)
class Tick:
    """What one tick did: summaries delivered, schedules fired, and any clock jump seen."""

    summaries: tuple[AlertIntent, ...] = ()
    fired: tuple[ScheduleFire, ...] = ()
    jump: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"summaries": [intent.to_dict() for intent in self.summaries], "fired": [fire.to_dict() for fire in self.fired], "jump": self.jump}


def _event_cursor(data: Any, event: Event) -> str | None:
    if isinstance(data, Mapping) and data.get("cursor") not in (None, ""):
        return str(data["cursor"])
    return event.subject_id if event.has_message else None


class Runner:
    """One tool's `watch run`: lock, replay, evaluate, deliver, tick (section 10.5).

    Step-driven so a tool can drive it from whatever loop its SDK needs:
    `start()`, then `replay(source)`, then `handle(event)` per event and
    `tick()` whenever nothing arrived, then `stop()`. `run(source)` is the
    foreground loop over a synchronous `EventSource`. `sync` is what a
    `SyncRequest` calls (the tool wraps `Archive.sync()` for its source);
    without one the request is recorded as unfulfilled. `wait` is how a
    rate-limit wait is honoured, injectable so the tests never sleep.
    """

    def __init__(
        self,
        archive: Archive,
        identity: Identity,
        rules: RuleSet,
        paths: ToolPaths,
        *,
        delivery: Delivery | None = None,
        sender: Any = None,
        allowlist: Iterable[str] | None = None,
        sync: Callable[[SyncRequest], Mapping[str, Any]] | None = None,
        queue: ReviewQueue | None = None,
        own_identities: Iterable[str] = (),
        clock: Clock | None = None,
        wait: Callable[[float], None] = time.sleep,
        tz: tzinfo | None = None,
        max_wait_s: float = 300.0,
    ) -> None:
        self.archive = archive
        self.identity = identity
        self.rules = rules
        self.paths = paths
        self.clock = clock or Clock()
        self.wait = wait
        self.max_wait_s = max_wait_s
        self.sync = sync
        if delivery is not None:
            self.platform_delivery = delivery.to_platform
        else:
            self.platform_delivery = PlatformDelivery(sender, identity, allowlist=allowlist) if sender is not None else None
        self.delivery = delivery or Delivery(platform=self.platform_delivery)
        self.state = RunnerState(archive, identity)
        self.schedules = Schedules(self.state, self.clock, tz)
        self.lock = Lock(paths.runner_lock)
        self.log = Log(paths.runner_log)
        self.engine = Engine(
            archive, identity, rules.rules, own_identities=own_identities, queue=queue, clock=self.clock.wall
        )
        self.baseline: Baseline | None = None
        self.own_prefix = _prefix(identity.id)
        self.started_at: str | None = None
        self.stopping = False
        self.reload_requested = False
        self.deliveries: deque[dict[str, Any]] = deque(maxlen=100)
        self.waits: deque[dict[str, Any]] = deque(maxlen=100)
        self.counts: dict[str, int] = {"events": 0, "refused": 0, "dropped": 0, "fired": 0, "replayed": 0, "syncs": 0, "unfulfilled_syncs": 0}
        self.jumps: deque[dict[str, Any]] = deque(maxlen=20)
        self._signals: dict[int, Any] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> LockInfo:
        """Take the lock, check the state version, record the baseline. RUNNER_LOCKED names a holder."""
        info = self.lock.acquire()
        try:
            self.state.check_version()
            self.started_at = info.started_at
            self._rebaseline("start")
        except Exception:
            self.lock.release()
            raise
        self.log.write("started", pid=info.pid, rules=len(self.rules), cursors=len(self.state.cursors()))
        return info

    def stop(self) -> None:
        if self.lock.held:
            self.log.write("stopped", pid=self.lock.pid)
        self.lock.release()

    def reload(self) -> int:
        """`watch reload`: re-read the rules directory; one bad file keeps the loaded set and is logged."""
        self.reload_requested = False
        try:
            loaded = self.rules.reload()
        except CodedError as exc:
            self.log.write("reload_failed", error=exc.code, message=exc.error.message)
            raise
        self.engine.rules = loaded
        self.log.write("reloaded", rules=len(loaded))
        return len(loaded)

    def request_stop(self, *_args: Any) -> None:
        self.stopping = True

    def request_reload(self, *_args: Any) -> None:
        self.reload_requested = True

    def install_signals(self) -> None:
        """SIGTERM and SIGINT stop after the current step; SIGHUP reloads the rules."""
        for number, handler in ((signal.SIGTERM, self.request_stop), (signal.SIGINT, self.request_stop), (signal.SIGHUP, self.request_reload)):
            self._signals[number] = signal.signal(number, handler)

    def restore_signals(self) -> None:
        for number, previous in self._signals.items():
            signal.signal(number, previous)
        self._signals.clear()

    def run(self, source: Any, *, install_signals: bool = True) -> dict[str, int]:
        """The foreground loop: start, replay, then events and ticks until asked to stop."""
        self.start()
        if install_signals:
            self.install_signals()
        try:
            self.replay(source)
            self.tick()
            for data in source.events():
                if self.stopping:
                    break
                if self.reload_requested:
                    try:
                        self.reload()
                    except CodedError:
                        pass
                if data is not None:
                    self.handle(data)
                self.tick()
                if self.stopping:
                    break
        finally:
            if install_signals:
                self.restore_signals()
            self.stop()
        return dict(self.counts)

    # -- events --------------------------------------------------------------

    def replay(self, source: Any) -> dict[str, int]:
        """Section 10.5: every scope with a cursor, from that cursor to now, through the engine with dedup on."""
        replayed: dict[str, int] = {}
        for rid, cursor in self.state.cursors().items():
            count = 0
            for data in source.replay(rid, cursor):
                self.handle(data)
                count += 1
            replayed[rid] = count
            self.counts["replayed"] += count
        self.log.write("replayed", scopes=replayed)
        return replayed

    def handle(self, data: Mapping[str, Any] | Event) -> Evaluation | None:
        """One event: refuse another platform's, evaluate, deliver, fulfil, then move the cursor."""
        event = data if isinstance(data, Event) else Event.from_dict(data)
        self.counts["events"] += 1
        same_platform = event.platform == self.identity.platform and _prefix(event.rid) == self.own_prefix
        if not same_platform:
            self.counts["refused"] += 1
            self.log.write("refused", rid=event.rid, platform=event.platform, reason="not this tool's platform")
            return None
        evaluation = self.engine.evaluate(event)
        if evaluation.dropped is not None:
            self.counts["dropped"] += 1
        self.counts["fired"] += len(evaluation.fired)
        for intent in evaluation.alerts:
            self.deliver(intent)
        for request in evaluation.syncs:
            self.fulfil(request)
        cursor = _event_cursor(data, event)
        if cursor is not None:
            self.state.save_cursor(event.rid, cursor)
        return evaluation

    def deliver(self, intent: AlertIntent) -> dict[str, Any]:
        """One intent through the delivery, honouring one rate-limit wait; every outcome is recorded for status."""
        record: dict[str, Any] = {"at": utc_now(), "rule": intent.rule_name, "destination": intent.destination.key, "folded": intent.folded}
        for attempt in (1, 2):
            try:
                result = self.delivery.deliver(intent)
            except RateLimited as limited:
                seconds = min(limited.retry_after_s, self.max_wait_s)
                self.waits.append({"at": utc_now(), "destination": intent.destination.key, "seconds": seconds, "platform": limited.error.platform})
                self.log.write("rate_limited", destination=intent.destination.key, seconds=seconds)
                if attempt == 1:
                    self.wait(seconds)
                    continue
                record.update(status="failed", error="RATE_LIMITED")
                break
            except CodedError as exc:
                record.update(status="refused" if exc.code in ("NOT_ALLOWLISTED", "PLATFORM_UNSUPPORTED") else "failed", error=exc.code)
                break
            else:
                status = str(result.get("status") or "ok")
                record.update(status=status, error=result.get("error"))
                break
        self.deliveries.append(record)
        self.log.write("delivered", **record)
        return record

    def fulfil(self, request: SyncRequest) -> dict[str, Any]:
        """A `SyncRequest` through the tool's sync callback; without one it is recorded as unfulfilled."""
        if self.sync is None:
            self.counts["unfulfilled_syncs"] += 1
            self.log.write("sync_unfulfilled", rid=request.rid, rule=request.rule_name)
            return {"status": "skipped", "rid": request.rid}
        try:
            result = dict(self.sync(request))
        except CodedError as exc:
            self.log.write("sync_failed", rid=request.rid, rule=request.rule_name, error=exc.code)
            return {"status": "failed", "rid": request.rid, "error": exc.code}
        self.counts["syncs"] += 1
        self.log.write("synced", rid=request.rid, rule=request.rule_name, status=result.get("status"))
        return result

    # -- the tick: summaries and schedules -------------------------------------

    def _rebaseline(self, reason: str) -> Baseline:
        self.baseline = Baseline(self.clock.wall(), self.clock.monotonic(), utc_now())
        self.state.set(BASELINE_KEY, {**self.baseline.to_dict(), "reason": reason})
        return self.baseline

    def tick(self) -> Tick:
        """Deliver every cooldown summary due, then fire every schedule due; detect a clock jump first."""
        if self.baseline is None:
            self._rebaseline("tick")
        assert self.baseline is not None
        wall, mono = self.clock.wall(), self.clock.monotonic()
        drift = wall - self.baseline.expected_wall(mono)
        jump: dict[str, Any] | None = None
        if drift < -BACKWARD_JUMP_S:
            jump = {"direction": "backward", "seconds": round(-drift, 3), "at": _iso(wall)}
            self._rebaseline("backward jump")
        summaries = self.engine.flush()
        for intent in summaries:
            self.deliver(intent)
        fired: list[ScheduleFire] = []
        for schedule in self.schedules.list():
            due_by_wall = schedule.next_wall <= wall
            due_by_monotonic = self.baseline.planned_monotonic(schedule.next_wall) <= mono
            if not (due_by_wall or due_by_monotonic):
                continue
            late = wall - schedule.next_wall > LATE_TOLERANCE_S
            fired.append(self._fire(schedule, wall, late))
        if drift > BACKWARD_JUMP_S:
            jump = {"direction": "forward", "seconds": round(drift, 3), "at": _iso(wall), "late_fires": sum(1 for fire in fired if fire.late)}
            self._rebaseline("forward jump")
        if jump is not None:
            self.jumps.append(jump)
            self.log.write("clock_jump", **jump)
        return Tick(summaries=summaries, fired=tuple(fired), jump=jump)

    def _fire(self, schedule: Schedule, wall: float, late: bool) -> ScheduleFire:
        """One firing, recorded before the send so a crash mid-send never fires it twice."""
        schedule.fires += 1
        schedule.last_fired_at = _iso(wall)
        schedule.last_late = late
        if schedule.every is not None:
            # One fire for every missed occurrence, then the next after now.
            schedule.next_wall = next_occurrence(schedule.every, max(wall, schedule.next_wall), self.schedules.tz)
            self.schedules.save(schedule)
        else:
            self.state.delete(schedule.key)
        status, error = "refused", "PLATFORM_UNSUPPORTED"
        if self.platform_delivery is not None:
            try:
                result = self.platform_delivery.send(schedule.rid, schedule.text)
                status, error = str(result.get("status") or "ok"), None
            except RateLimited as limited:
                seconds = min(limited.retry_after_s, self.max_wait_s)
                self.waits.append({"at": utc_now(), "destination": f"platform:{schedule.rid}", "seconds": seconds, "platform": limited.error.platform})
                self.wait(seconds)
                try:
                    result = self.platform_delivery.send(schedule.rid, schedule.text)
                    status, error = str(result.get("status") or "ok"), None
                except CodedError as exc:
                    status, error = "failed", exc.code
            except CodedError as exc:
                status, error = ("refused" if exc.code in ("NOT_ALLOWLISTED", "PLATFORM_UNSUPPORTED") else "failed"), exc.code
        fire = ScheduleFire(schedule.id, schedule.rid, _iso(wall), late, status, error)
        self.log.write("schedule_fired", **fire.to_dict())
        return fire

    # -- status --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """The live runner's view: everything `report()` shows plus the engine, waits and deliveries."""
        base = report(self.paths, self.archive)
        base.update(
            {
                "running": self.lock.held,
                "started_at": self.started_at,
                "counts": dict(self.counts),
                "engine": self.engine.status(),
                "waits": list(self.waits),
                "deliveries": list(self.deliveries),
                "jumps": list(self.jumps),
            }
        )
        return base


# -- watch status, stop and reload from another process ------------------------


def report(paths: ToolPaths, archive: Archive | None = None) -> dict[str, Any]:
    """`watch status`: the lock and its holder, the state table, and the last 20 log lines."""
    holder = read_lock(paths.runner_lock)
    out: dict[str, Any] = {
        "lock": None if holder is None else holder.to_dict(),
        "running": bool(holder and holder.alive),
        "log": tail(paths.runner_log, STATUS_LOG_LINES),
        "guarantees": list(GUARANTEES),
    }
    if archive is not None:
        rows = archive.connection.execute("SELECT key, identity_id, value, updated_at FROM runner_state ORDER BY key").fetchall()
        state = {row[0]: {"identity_id": row[1], "value": _loads(row[2]), "updated_at": row[3]} for row in rows}
        out["state"] = state
        out["state_version"] = state.get(VERSION_KEY, {}).get("value")
        out["cursors"] = {key[len(CURSOR_KEY):]: entry["value"] for key, entry in state.items() if key.startswith(CURSOR_KEY)}
        out["schedules"] = [listing(Schedule.from_dict(entry["value"])) for key, entry in state.items() if key.startswith(SCHEDULE_KEY)]
        out["windows"] = [entry["value"] for key, entry in state.items() if key.startswith("cooldown:")]
    return out


def _loads(text: Any) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def holder_or_refuse(paths: ToolPaths) -> LockInfo:
    holder = read_lock(paths.runner_lock)
    if holder is None or not holder.alive:
        raise CodedError("RUNNER_NOT_RUNNING", "no runner holds the lock", hint="`watch run` starts one")
    return holder


def stop(paths: ToolPaths, *, wait_s: float = STOP_WAIT_S, kill: Callable[[int, int], None] = os.kill, sleep: Callable[[float], None] = time.sleep, poll_s: float = 0.2) -> dict[str, Any]:
    """`watch stop`: SIGTERM to the holder, then wait up to `wait_s` for the lock to clear."""
    holder = holder_or_refuse(paths)
    kill(holder.pid, signal.SIGTERM)
    waited = 0.0
    while waited < wait_s:
        current = read_lock(paths.runner_lock)
        if current is None or not current.alive:
            return {"status": "ok", "pid": holder.pid, "waited_s": round(waited, 3)}
        sleep(poll_s)
        waited += poll_s
    return {"status": "partial", "pid": holder.pid, "waited_s": round(waited, 3), "error": "the runner has not exited yet"}


def reload(paths: ToolPaths, *, kill: Callable[[int, int], None] = os.kill) -> dict[str, Any]:
    """`watch reload`: SIGHUP to the holder, which re-reads its rules on the next step."""
    holder = holder_or_refuse(paths)
    kill(holder.pid, signal.SIGHUP)
    return {"status": "ok", "pid": holder.pid}


def runner_queries(connection: Any) -> list[str]:
    """Every way the runner's rows disagree with sections 10.5, 10.6 and 17; empty means they agree."""
    failures: list[str] = []
    rows = connection.execute("SELECT key, value FROM runner_state").fetchall()
    for key, value in rows:
        if key == VERSION_KEY:
            parsed = _loads(value)
            if not isinstance(parsed, int) or parsed < 1:
                failures.append(f"runner_state version {value!r} is not a positive whole number")
            elif parsed > RUNNER_STATE_VERSION:
                failures.append(f"runner_state version {parsed} is newer than this build's {RUNNER_STATE_VERSION}")
        elif key.startswith(SCHEDULE_KEY):
            parsed = _loads(value)
            try:
                schedule = Schedule.from_dict(parsed)
            except (KeyError, TypeError, ValueError):
                failures.append(f"{key} does not parse as a schedule")
                continue
            if schedule.guarantee not in GUARANTEES and not schedule.guarantee.startswith("runner-held"):
                failures.append(f"{key} carries guarantee {schedule.guarantee!r}")
            if (schedule.at is None) == (schedule.every is None):
                failures.append(f"{key} is neither once nor repeating")
        elif key.startswith(CURSOR_KEY):
            parsed = _loads(value)
            if not isinstance(parsed, Mapping) or "cursor" not in parsed:
                failures.append(f"{key} carries no cursor")
            else:
                try:
                    parse_rid(key[len(CURSOR_KEY):])
                except RidError as exc:
                    failures.append(f"{key}: {exc}")
    return failures
