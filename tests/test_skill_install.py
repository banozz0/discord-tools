"""`skill install`, `doctor`'s skill line and row 9's install row, from the outside.

A skill is a file in an agent's folder, not something on Discord, so none of
this logs in, and it has to work before `auth` has ever run. What a user sees
is checked: the file on disk, the envelope, the exit code and the audit line.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from discord_tools import agent_skill
from discord_tools import profiles as profile_store
from discord_tools.cli import build_parser, main, run
from discord_tools.config import save_token
from discord_tools.doctor import check_skill
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.menu import run_menu

BOT_42 = "NDI.fake.sig"
BUNDLED = agent_skill.bundled_text()
BUNDLED_VERSION = next(line.split(":", 1)[1].strip() for line in BUNDLED.splitlines() if line.startswith("version:"))


def go(argv, *, isatty=True):
    args = build_parser().parse_args(argv)
    out = Run(command_name(args), echoed_args(args), json=True, stdout=io.StringIO(), stderr=io.StringIO(), isatty=isatty, presents=True)
    code = asyncio.run(run(args, out=out))
    return code, json.loads(out.stdout.getvalue()), out.stderr.getvalue()


def answer(monkeypatch, text):
    monkeypatch.setattr("builtins.input", lambda _prompt="": text)


def skill_file(version: str) -> str:
    return f"---\nname: discord-tools\nversion: {version}\n---\n\nAn older copy.\n"


def signed_in(name="default"):
    save_token(name, BOT_42)
    profile_store.remember(name, label="testbot#0 (profile default)", bot_id=42)


def audit_lines(home: Path) -> list[dict]:
    path = home / ".discord-tools" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# -- the command ------------------------------------------------------------


def test_a_fresh_install_writes_the_bundled_skill_and_signs_it(home_is_a_tmp_dir, tmp_path):
    signed_in()
    folder = tmp_path / "skills" / "discord-tools"

    code, envelope, _ = go(["--json", "skill", "install", "--dir", str(folder), "--yes"])

    assert code == 0
    assert (folder / "SKILL.md").read_text(encoding="utf-8") == BUNDLED
    assert oct((folder / "SKILL.md").stat().st_mode & 0o777) == "0o644"
    assert envelope["status"] == "ok"
    assert envelope["command"] == "skill install"
    assert envelope["args"] == {"skill_dir": str(folder), "yes": True}
    assert envelope["result"] == {
        "path": str(folder / "SKILL.md"),
        "action": "created",
        "installed_version": None,
        "bundled_version": BUNDLED_VERSION,
        "cancelled": False,
    }
    assert "reads back identical to the bundled skill" in envelope["evidence"]["readback"]
    assert envelope["plan"]["approval"] == "prompt_y"
    [line] = audit_lines(home_is_a_tmp_dir)
    assert line["command"] == "skill install"
    assert line["status"] == "ok"
    assert line["plan_id"] == envelope["plan"]["plan_id"]


def test_the_default_folder_is_claude_codes(home_is_a_tmp_dir):
    code, envelope, _ = go(["--json", "skill", "install", "--yes"])

    target = home_is_a_tmp_dir / ".claude" / "skills" / "discord-tools" / "SKILL.md"
    assert code == 0
    assert target.read_text(encoding="utf-8") == BUNDLED
    assert envelope["result"]["path"] == str(target)


def test_with_no_profile_it_still_installs_unsigned_and_leaves_no_audit_line(home_is_a_tmp_dir, tmp_path, capsys):
    folder = tmp_path / "discord-tools"

    code = main(["--json", "skill", "install", "--dir", str(folder), "--yes"])

    envelope = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (folder / "SKILL.md").exists()
    assert envelope["plan"] is None
    assert envelope["identity"] is None
    assert any("unsigned" in warning and "auth" in warning for warning in envelope["warnings"])
    assert audit_lines(home_is_a_tmp_dir) == []


def test_a_tilde_in_dir_is_the_home_folder(home_is_a_tmp_dir, monkeypatch):
    monkeypatch.setenv("HOME", str(home_is_a_tmp_dir))

    code, envelope, _ = go(["--json", "skill", "install", "--dir", "~/agent/discord-tools", "--yes"])

    assert code == 0
    assert (home_is_a_tmp_dir / "agent" / "discord-tools" / "SKILL.md").exists()


def test_an_identical_file_asks_nothing_and_writes_nothing(home_is_a_tmp_dir, tmp_path, monkeypatch):
    signed_in()
    folder = tmp_path / "discord-tools"
    folder.mkdir()
    (folder / "SKILL.md").write_text(BUNDLED, encoding="utf-8")
    before = (folder / "SKILL.md").stat().st_mtime_ns
    monkeypatch.setattr("builtins.input", lambda _prompt="": pytest.fail("an identical file asked a question"))

    code, envelope, stderr = go(["--json", "skill", "install", "--dir", str(folder)])

    assert code == 0
    assert envelope["status"] == "ok"
    assert envelope["result"]["action"] == "unchanged"
    assert (folder / "SKILL.md").stat().st_mtime_ns == before
    assert "Nothing to do" in stderr
    assert audit_lines(home_is_a_tmp_dir) == []


def test_an_older_copy_is_previewed_with_both_versions_then_replaced(home_is_a_tmp_dir, tmp_path, monkeypatch):
    folder = tmp_path / "discord-tools"
    folder.mkdir()
    (folder / "SKILL.md").write_text(skill_file("0.1.0"), encoding="utf-8")
    answer(monkeypatch, "y")

    code, envelope, stderr = go(["--json", "skill", "install", "--dir", str(folder)])

    assert code == 0
    assert "Installed  0.1.0" in stderr
    assert f"Bundled    {BUNDLED_VERSION}" in stderr
    assert "Action     updated" in stderr
    assert envelope["result"]["installed_version"] == "0.1.0"
    assert (folder / "SKILL.md").read_text(encoding="utf-8") == BUNDLED


def test_a_newer_copy_says_installing_goes_back(home_is_a_tmp_dir, tmp_path, monkeypatch):
    folder = tmp_path / "discord-tools"
    folder.mkdir()
    (folder / "SKILL.md").write_text(skill_file("999.0.0"), encoding="utf-8")
    answer(monkeypatch, "n")

    code, envelope, stderr = go(["--json", "skill", "install", "--dir", str(folder)])

    assert code == 1
    assert envelope["status"] == "cancelled"
    assert f"The installed skill is newer than this release's; installing goes back to {BUNDLED_VERSION}." in stderr
    assert (folder / "SKILL.md").read_text(encoding="utf-8") == skill_file("999.0.0")


def test_no_terminal_and_no_yes_is_approval_required(home_is_a_tmp_dir, tmp_path):
    folder = tmp_path / "discord-tools"

    code, envelope, _ = go(["--json", "skill", "install", "--dir", str(folder)], isatty=False)

    assert code == 3
    assert envelope["error"]["code"] == "APPROVAL_REQUIRED"
    assert not folder.exists()


def test_a_symlinked_folder_is_managed_by_hand_and_refused(home_is_a_tmp_dir, tmp_path):
    real = tmp_path / "repo-skill"
    real.mkdir()
    (real / "SKILL.md").write_text(skill_file("0.1.0"), encoding="utf-8")
    link = tmp_path / "skills" / "discord-tools"
    link.parent.mkdir()
    link.symlink_to(real)

    code, envelope, _ = go(["--json", "skill", "install", "--dir", str(link), "--yes"])

    assert code == 2
    assert envelope["error"]["code"] == "TARGET_KIND_MISMATCH"
    assert (real / "SKILL.md").read_text(encoding="utf-8") == skill_file("0.1.0")


def test_a_symlinked_file_is_refused(home_is_a_tmp_dir, tmp_path):
    folder = tmp_path / "discord-tools"
    folder.mkdir()
    real = tmp_path / "elsewhere.md"
    real.write_text(skill_file("0.1.0"), encoding="utf-8")
    (folder / "SKILL.md").symlink_to(real)

    code, envelope, _ = go(["--json", "skill", "install", "--dir", str(folder), "--yes"])

    assert code == 2
    assert envelope["error"]["code"] == "TARGET_KIND_MISMATCH"
    assert real.read_text(encoding="utf-8") == skill_file("0.1.0")


def test_a_local_md_beside_the_skill_is_left_alone(home_is_a_tmp_dir, tmp_path):
    folder = tmp_path / "discord-tools"
    folder.mkdir()
    (folder / "SKILL.md").write_text(skill_file("0.1.0"), encoding="utf-8")
    (folder / "LOCAL.md").write_text("my own notes\n", encoding="utf-8")

    code, _, _ = go(["--json", "skill", "install", "--dir", str(folder), "--yes"])

    assert code == 0
    assert (folder / "LOCAL.md").read_text(encoding="utf-8") == "my own notes\n"
    assert sorted(entry.name for entry in folder.iterdir()) == ["LOCAL.md", "SKILL.md"]


def test_a_store_others_can_read_refuses_like_every_local_write(home_is_a_tmp_dir, tmp_path):
    signed_in()
    (home_is_a_tmp_dir / ".discord-tools" / ".env").chmod(0o644)
    folder = tmp_path / "discord-tools"

    code, envelope, _ = go(["--json", "skill", "install", "--dir", str(folder), "--yes"])

    assert code == 2
    assert envelope["status"] == "refused"
    assert "chmod go-rwx" in envelope["error"]["message"]
    assert not folder.exists()


def test_the_repo_copy_is_read_when_the_package_carries_none():
    # This checkout is an editable install: the package holds no skill/ folder,
    # so the loader falls back to the repo's own file, the one source.
    repo_copy = Path(__file__).resolve().parents[1] / "skill" / "SKILL.md"
    assert BUNDLED == repo_copy.read_text(encoding="utf-8")


# -- doctor -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("installed", "status", "words"),
    [
        (None, "OK", "not installed"),
        (BUNDLED, "OK", "current"),
        (skill_file("0.1.0"), "WARN", "updates it"),
        (skill_file("999.0.0"), "OK", "is newer than"),
    ],
)
def test_doctor_says_where_the_skill_stands(home_is_a_tmp_dir, installed, status, words):
    folder = home_is_a_tmp_dir / ".claude" / "skills" / "discord-tools"
    if installed is not None:
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(installed, encoding="utf-8")

    check = check_skill(home=home_is_a_tmp_dir)

    assert check.status == status
    assert check.message.startswith("Agent skill: ")
    assert words in check.message


def test_doctor_calls_a_linked_folder_managed_by_hand(home_is_a_tmp_dir, tmp_path):
    real = tmp_path / "repo-skill"
    real.mkdir()
    root = home_is_a_tmp_dir / ".claude" / "skills"
    root.mkdir(parents=True)
    (root / "discord-tools").symlink_to(real)

    check = check_skill(home=home_is_a_tmp_dir)

    assert check.status == "OK"
    assert "managed by hand" in check.message


def test_the_skill_line_never_fails_doctor(home_is_a_tmp_dir, capsys):
    folder = home_is_a_tmp_dir / ".claude" / "skills" / "discord-tools"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(skill_file("0.1.0"), encoding="utf-8")

    main(["--json", "doctor"])

    checks = json.loads(capsys.readouterr().out)["result"]["checks"]
    [line] = [check for check in checks if check["message"].startswith("Agent skill: ")]
    assert line["status"] != "FAIL"


# -- row 9 ----------------------------------------------------------------


def run_the_menu(answers, profile=None):
    queue = list(answers)
    screens = []

    def read(prompt=""):
        screens.append(prompt)
        return queue.pop(0)

    code = asyncio.run(run_menu(profile=profile, read=read, write=screens.append))
    return code, "\n".join(str(screen) for screen in screens)


def test_row_9_installs_the_skill_into_the_default_folder_after_the_clis_own_y_n(home_is_a_tmp_dir, monkeypatch, capsys):
    answer(monkeypatch, "y")

    code, drawn = run_the_menu(["9", "3", "", "0"])

    target = home_is_a_tmp_dir / ".claude" / "skills" / "discord-tools" / "SKILL.md"
    assert code == 0
    assert "3. Install the agent skill" in drawn
    assert "Skill folder [~/.claude/skills/discord-tools] (Enter uses it): " in drawn
    assert f"File       {target}" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == BUNDLED


def test_row_9_takes_a_typed_folder_and_a_no_writes_nothing(home_is_a_tmp_dir, tmp_path, monkeypatch, capsys):
    answer(monkeypatch, "n")
    folder = tmp_path / "codex" / "discord-tools"

    code, _ = run_the_menu(["9", "3", str(folder), "0"])

    assert code == 0
    assert f"File       {folder / 'SKILL.md'}" in capsys.readouterr().out
    assert not folder.exists()


def test_row_9_signs_the_install_as_the_menus_own_profile(home_is_a_tmp_dir, monkeypatch):
    signed_in("harry")
    answer(monkeypatch, "y")

    code, _ = run_the_menu(["9", "3", "", "0"], profile="harry")

    assert code == 0
    [line] = audit_lines(home_is_a_tmp_dir)
    assert line["command"] == "skill install"
    assert line["identity"]["profile"] == "harry"
