"""The scanner adapter: a local scanner's word on a quarantined file, or the honest lack of one.

Spec: section 9.4. `ScannerAdapter` is the Protocol the pipeline's last check
calls with the complete payload, and `ClamAVAdapter` is the one implementation
in the main version: `clamdscan --fdpass --no-summary <path>` when `clamdscan`
is on PATH, else `clamscan --no-summary <path>`. Exit 0 is `CLEAN`, exit 1 is
`INFECTED` with the signature name recorded, anything else is `UNSCANNED` with
the reason, and no binary on PATH is `UNSCANNED` naming the binaries looked
for, which is also what `doctor` prints through `report()`.

A scanner here only ever runs a local process over a local path. There is no
upload, no hash lookup and no reputation service in any code path: a future
sandbox or reputation integration is a separately approved design (section
20, gate G4) and would be a second adapter, never a default.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

DEFAULT_TIMEOUT = 300.0


@dataclass(frozen=True)
class ScanResult:
    """What a scanner said: the verdict, why, which scanner, and the signature when infected."""

    verdict: str
    detail: str
    scanner: str
    signature: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in ("CLEAN", "INFECTED", "UNSCANNED"):
            raise ValueError(f"a scanner says CLEAN, INFECTED or UNSCANNED, not {self.verdict!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "detail": self.detail, "scanner": self.scanner, "signature": self.signature}


@runtime_checkable
class ScannerAdapter(Protocol):
    """One local scanner. `scan` never raises for a scanner problem: that is an UNSCANNED result."""

    def scan(self, path: Path) -> ScanResult:
        """The scanner's verdict on the file at `path`, a complete payload in quarantine."""
        ...

    def report(self) -> dict[str, Any]:
        """What `doctor` prints: which binary would run, and which were looked for."""
        ...


# In the order they are tried. `--fdpass` hands clamd an open descriptor so the
# daemon reads the 0600 payload without needing rights on the directory.
BINARIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("clamdscan", ("--fdpass", "--no-summary")),
    ("clamscan", ("--no-summary",)),
)


class ClamAVAdapter:
    """ClamAV from PATH, as section 9.4 maps it. `which` and `run` are injectable for tests."""

    name = "clamav"

    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        run: Callable[..., Any] = subprocess.run,
        timeout: float = DEFAULT_TIMEOUT,
        binaries: Sequence[tuple[str, tuple[str, ...]]] = BINARIES,
    ) -> None:
        self.which = which
        self.run = run
        self.timeout = timeout
        self.binaries = tuple(binaries)

    @property
    def looked_for(self) -> tuple[str, ...]:
        return tuple(name for name, _flags in self.binaries)

    def locate(self) -> tuple[str, str, tuple[str, ...]] | None:
        """(name, path, flags) of the first binary on PATH, or None."""
        for name, flags in self.binaries:
            found = self.which(name)
            if found:
                return name, found, flags
        return None

    def report(self) -> dict[str, Any]:
        located = self.locate()
        return {
            "scanner": self.name,
            "binary": located[1] if located else None,
            "command": located[0] if located else None,
            "looked_for": list(self.looked_for),
        }

    def scan(self, path: Path) -> ScanResult:
        located = self.locate()
        if located is None:
            return ScanResult(
                "UNSCANNED",
                f"no scanner on PATH: looked for {', '.join(self.looked_for)}",
                self.name,
            )
        name, binary, flags = located
        command = [binary, *flags, str(path)]
        try:
            completed = self.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ScanResult("UNSCANNED", f"{name} gave no answer in {self.timeout:.0f} s", self.name)
        except OSError as error:
            return ScanResult("UNSCANNED", f"{name} could not run: {error}", self.name)
        code = int(completed.returncode)
        stdout = str(completed.stdout or "")
        stderr = str(completed.stderr or "")
        if code == 0:
            return ScanResult("CLEAN", f"{name} found nothing", self.name)
        if code == 1:
            signature = parse_signature(stdout) or "unnamed signature"
            return ScanResult("INFECTED", f"{name}: {signature}", self.name, signature=signature)
        reason = _first_line(stderr) or _first_line(stdout) or "no output"
        return ScanResult("UNSCANNED", f"{name} exited {code}: {reason}", self.name)


def parse_signature(stdout: str) -> str | None:
    """The signature name from a `<path>: <Name> FOUND` line, the first one when there are several."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.endswith(" FOUND"):
            continue
        body = line[: -len(" FOUND")]
        _path, separator, signature = body.rpartition(": ")
        return (signature if separator else body).strip() or None
    return None


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""
