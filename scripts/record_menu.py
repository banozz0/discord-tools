#!/usr/bin/env python3
"""Record the menu into docs/transcripts/, for the site's terminal replay.

The menu code, the colour and the prompt rendering are the real ones; only the
Discord side is canned, so the recording is a real session against a fake
server rather than a mock-up of one. `input` is replaced by a reader that
prints the prompt and the answer, which is both how the keystrokes get in and
how they end up in the file — the site's parser reads `Choose: 2` as a prompt
and the character after it as the keystroke to replay.

    .venv/bin/python scripts/record_menu.py --write   # all three files
    .venv/bin/python scripts/record_menu.py           # just print the session

Run it from the virtualenv: the canned client is the same `tests/conftest.py`
fake every test mocks, so the recording cannot drift from what the suite proves,
and importing it needs pytest on the path.

`--write` records through `script` because colour only turns on when stdout is
a terminal, cleans the pty's own control bytes out of the capture, and writes
the escape-stripped copy and the NO_COLOR one beside it. Read the diff, then
hand the three files to the site and re-check its screen indices.

Colour only turns on when stdout is a terminal, so the `.ansi` file has to be
recorded through `script`. Channel names carry emoji because that is what they
look like in real servers — but never one with a variation selector: this
terminal draws `⚙️` one column wide and a browser draws it two, so a name with
one lines up here and not on the site.
"""

from __future__ import annotations

import asyncio
import builtins
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

# Before any import of the tool: every confirm takes its reader as a default
# argument, and a default is bound when the function is defined. Patching after
# the imports would leave those gates reading the real stdin and the recording
# would hang on the first y/N.
_answers = iter(())


def _read(prompt: str = "") -> str:
    """Print the prompt with its answer, and hand the answer back.

    This is both how the keystrokes get in and how they end up in the file: the
    site's parser reads `Choose: 2` as a prompt and the rest of the line as the
    keystroke to replay.
    """
    answer = next(_answers)
    sys.stdout.write(f"{prompt}{answer}\n")
    sys.stdout.flush()
    return answer


builtins.input = _read

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from conftest import FakeClient  # noqa: E402
from discord_tools import __version__  # noqa: E402
from discord_tools.config import Config  # noqa: E402
from discord_tools.menu import MenuSession, run_menu  # noqa: E402
from discord_tools.models import BotIdentity, ChannelInfo, MemberInfo, ServerInfo, ThreadInfo  # noqa: E402

IDENTITY = BotIdentity(
    id=1394820000002,
    username="harrybot",
    application_id=1394820000002,
    message_content_intent="enabled",
    description="the agency's reporter",
    has_avatar=True,
)

SERVER = ServerInfo(id=1394820000000, name="Agency")
CHANNELS = [
    ChannelInfo(id=1394827364077, name="Ops stuff", type="category"),
    ChannelInfo(id=1394827364512, name="🚨alerts", type="text", parent_id=1394827364077),
    ChannelInfo(id=1394827364598, name="📰briefings", type="text", parent_id=1394827364077),
]
THREADS = [
    ThreadInfo(id=1394829911004, name="📚vault-alerts", parent_id=1394827364512),
    ThreadInfo(id=1394829911077, name="🔧system-alerts", parent_id=1394827364512),
]


class Message:
    """A history row, in the shape the search path reads."""

    def __init__(self, id: int, created: datetime, author: str, content: str) -> None:
        self.id = id
        self.created_at = created
        self.author = type("Author", (), {"name": author, "id": 1394820000001, "bot": False})()
        self.content = content
        self.attachments: list = []
        self.embeds: list = []


HISTORY = [
    Message(
        1394829911100,
        datetime(2026, 9, 5, 21, 40, tzinfo=UTC),
        "Harry",
        "Deploy 4.2 is out: 6 checks green, 0 rollbacks, 41s.",
    ),
    Message(
        1394829911042,
        datetime(2026, 9, 5, 15, 0, tzinfo=UTC),
        "Harry",
        "Deploy queue: 3 waiting, oldest 11 minutes.",
    ),
]

# The walk, one answer per prompt. Section 14's numbers: 2 Read, 4 Build,
# 5 Clear messages, 8 Identity.
KEYS = [
    "2", "1",                      # Read -> Search / export messages
    "1",                           # the one server auto-picks; 🚨alerts
    "1", "deploy",                 # Contains -> deploy
    "6",                           # Run it (print here)
    "",                            # Enter -> main menu
    "8", "1", "1", "",             # Identity -> Profiles -> List them -> menu
    "4", "1",                      # Build -> Create
    "1", "1", "releases", "2",     # Channel -> Text channel -> name -> No category
    "y",                           # the create gate, answered at the prompt
    "",                            # Enter -> main menu
    "5", "1", "1",                 # Clear messages -> one channel -> 🚨alerts
    "0", "0",                      # out of the dry run, out of clear
    "0",                           # exit
]


def a_client() -> FakeClient:
    return FakeClient(
        identity=IDENTITY,
        servers=[SERVER],
        channels={SERVER.id: CHANNELS},
        threads={SERVER.id: THREADS},
        channel_info={channel.id: channel for channel in CHANNELS},
        history={1394827364512: HISTORY},
        members={SERVER.id: [MemberInfo(id=1394820000001, username="sven", display_name="Sven")]},
    )


def sandbox() -> Path:
    """A throwaway ~/.discord-tools holding two invented profiles.

    Without this the recording reads the real store, and the profiles screen
    would put somebody's actual profile names into a file that ships with the
    site. Nothing here is a real name and nothing here is a real token.
    """
    from discord_tools import profiles as profile_store

    home = Path(tempfile.mkdtemp(prefix="discord-tools-recording-"))
    Path.home = classmethod(lambda cls: home)  # type: ignore[method-assign]
    for name in [key for key in os.environ if key.startswith("DISCORD_")]:
        del os.environ[name]

    profile_store.remember("harry", label="harrybot (profile harry)", bot_id=IDENTITY.id, home=home)
    profile_store.remember("dobby", label="dobbybot (profile dobby)", bot_id=1394820000003, home=home)
    return home


HEADER = """\
discord-tools {version} — the menu, recorded in a real terminal (pty, TERM=xterm-256color)
{rule}
Recorded by scripts/record_menu.py on {commit}. The menu code, the colour and the
prompt rendering are the real ones; the Discord side is a canned session (no network, no
token), so the server, channels, threads and messages below are invented. Keystrokes
appear after each prompt. The .ansi sibling of this file is the same run with the colour
escapes kept.

"""

_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def clean(raw: bytes) -> str:
    """The capture without the pty's own noise.

    `script` echoes the EOF that closed stdin as a literal `^D`, writes CR
    before every LF, and leaves the odd backspace behind. None of it is
    anything the menu printed, and the site's parser should not have to know
    which terminal recorded the file.
    """
    text = raw.decode("utf-8", "replace").replace("\x04", "").replace("\b", "").replace("\r", "")
    return text.removeprefix("^D")


def write_transcripts() -> int:
    """Record all three files into docs/transcripts/ and say where they went."""
    root = Path(__file__).resolve().parents[1]
    out = root / "docs" / "transcripts"
    out.mkdir(parents=True, exist_ok=True)
    stem = f"discord-tools-{__version__}"
    ansi = out / f"{stem}-menu.ansi"

    environment = {**os.environ, "TERM": "xterm-256color"}
    environment.pop("NO_COLOR", None)
    subprocess.run(
        ["script", "-q", str(ansi), sys.executable, __file__],
        check=True,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    session = clean(ansi.read_bytes())
    ansi.write_text(session, encoding="utf-8")

    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    header = HEADER.format(version=__version__, rule="=" * 85, commit=commit or "an uncommitted tree")
    (out / f"{stem}-menu.txt").write_text(header + _ESCAPE.sub("", session), encoding="utf-8")

    plain = subprocess.run(
        [sys.executable, __file__, "--root-only"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "NO_COLOR": "1"},
        stdin=subprocess.DEVNULL,
    ).stdout
    (out / f"{stem}-nocolor.txt").write_text(
        f"discord-tools {__version__} with NO_COLOR=1 — same code, same terminal, plain text "
        f"(root screen, then exit)\n{plain}",
        encoding="utf-8",
    )
    for name in (f"{stem}-menu.ansi", f"{stem}-menu.txt", f"{stem}-nocolor.txt"):
        print(f"wrote {out / name}")
    return 0


def main() -> int:
    global _answers

    sandbox()
    _answers = iter(["0"] if "--root-only" in sys.argv else KEYS)
    session = MenuSession(
        config=Config(
            # Never read: the seam is the canned client, injected below.
            token="unused",
            profile="harry",
            tokens={"harry": "unused", "dobby": "unused"},
            send_allowlist=(1394827364512,),
        ),
        profile="harry",
    )
    session._client = a_client()
    return asyncio.run(run_menu(session=session))


if __name__ == "__main__":
    raise SystemExit(write_transcripts() if "--write" in sys.argv else main())
