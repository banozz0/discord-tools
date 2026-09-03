"""Guards the vendored shared-code copy under src/<package>/_core/.

A tool copies this file into its tests/ unchanged. It finds the one _core/
under src/, recomputes the tree hash exactly as scripts/sync-core.sh did and
compares it with _core/VERSION, so any local edit fails here: fixes go to the
workshop first and are re-synced. It then runs the vendored conformance
fixtures under the tool's own package name, so a shape change in the copy
fails the tool's own tests rather than a user's script.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def vendored_core() -> Path:
    hits = sorted(SRC.glob("*/_core/VERSION"))
    assert len(hits) == 1, f"expected one src/<package>/_core/VERSION, found {hits}"
    return hits[0].parent


def tree_hash(root: Path) -> str:
    """sha256 over "<sha256 of file>  <relative path>\\n" for every file, sorted by path."""
    entries = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in ("VERSION", "README") or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        entries.append((rel, path))
    digest = hashlib.sha256()
    for rel, path in sorted(entries):
        digest.update(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}\n".encode())
    return digest.hexdigest()


def read_version(core: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in (core / "VERSION").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(" ")
        fields[key] = value.strip()
    return fields


def test_version_names_a_tag_a_commit_and_a_tree():
    version = read_version(vendored_core())
    assert set(version) == {"tag", "commit", "tree"}
    assert re.fullmatch(r"v[0-9][0-9A-Za-z.-]*", version["tag"]), version["tag"]
    assert re.fullmatch(r"[0-9a-f]{40}", version["commit"]), version["commit"]
    assert re.fullmatch(r"[0-9a-f]{64}", version["tree"]), version["tree"]


def test_copy_matches_its_recorded_tree_hash():
    core = vendored_core()
    assert tree_hash(core) == read_version(core)["tree"], (
        "the vendored copy differs from the workshop at its tag; fixes go to the workshop, then scripts/sync-core.sh"
    )


def test_readme_is_the_one_line_warning():
    lines = (vendored_core() / "README").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "do not edit here" in lines[0]


def test_the_copy_names_no_url():
    # The workshop is private: the copy records a tag, a sha and a hash, never where it came from.
    for name in ("VERSION", "README"):
        text = (vendored_core() / name).read_text(encoding="utf-8")
        assert not re.search(r"[a-z]+://|\w+@\w+\.\w+", text), f"{name} names a location"


def test_vendored_fixtures_conform():
    core = vendored_core()
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    conformance = importlib.import_module(f"{core.parent.name}._core.conformance")
    assert conformance.run() == []
