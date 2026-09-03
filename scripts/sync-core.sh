#!/usr/bin/env bash
# sync-core.sh — refresh this tool's vendored copy of the shared core at a tag.
#
#   CLI_TOOLS_CORE_PATH=/path/to/workshop scripts/sync-core.sh v0.1
#
# Reads the workshop from the local checkout named by CLI_TOOLS_CORE_PATH (no
# URL is ever written into a tool), exports the tree the tag points at with
# git archive, copies it byte for byte into src/<package>/_core/, and writes
# _core/VERSION (tag, commit, tree hash) plus the one-line _core/README.
# Rewrites nothing inside the tree. Tags can move and shas cannot: a re-run
# against the same tag refuses when the workshop's tag now points at a
# different commit. tests/test_core_copy.py recomputes the tree hash and
# fails on any local edit; fixes go to the workshop first, then re-sync.
set -euo pipefail

usage() { echo "usage: CLI_TOOLS_CORE_PATH=<workshop checkout> $0 <tag>" >&2; exit 64; }
[ $# -eq 1 ] || usage
TAG=$1
WORKSHOP=${CLI_TOOLS_CORE_PATH:-}
[ -n "$WORKSHOP" ] || { echo "sync-core: CLI_TOOLS_CORE_PATH is not set; it names the local workshop checkout" >&2; exit 64; }
git -C "$WORKSHOP" rev-parse --git-dir >/dev/null 2>&1 || { echo "sync-core: $WORKSHOP is not a git checkout" >&2; exit 64; }

TOOL_ROOT=$(cd "$(dirname "$0")/.." && pwd)
PKG_DIR=""
count=0
for init in "$TOOL_ROOT"/src/*/__init__.py; do
  [ -e "$init" ] || continue
  PKG_DIR=$(dirname "$init")
  count=$((count + 1))
done
[ "$count" -eq 1 ] || { echo "sync-core: expected exactly one package under src/, found $count" >&2; exit 64; }
DEST=$PKG_DIR/_core

SHA=$(git -C "$WORKSHOP" rev-parse --verify --quiet "refs/tags/$TAG^{commit}") \
  || { echo "sync-core: no tag $TAG in $WORKSHOP" >&2; exit 2; }

if [ -f "$DEST/VERSION" ]; then
  old_tag=$(awk '$1=="tag"{print $2}' "$DEST/VERSION")
  old_sha=$(awk '$1=="commit"{print $2}' "$DEST/VERSION")
  if [ "$old_tag" = "$TAG" ] && [ -n "$old_sha" ] && [ "$old_sha" != "$SHA" ]; then
    echo "sync-core: refusing: tag $TAG moved. VERSION records commit $old_sha; the workshop's tag now points at $SHA. Tags can move, shas cannot: re-tag the workshop or pick another tag." >&2
    exit 2
  fi
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git -C "$WORKSHOP" archive --format=tar "$SHA" src/cli_tools_core | tar -x -C "$TMP"
SRC=$TMP/src/cli_tools_core
[ -d "$SRC" ] || { echo "sync-core: tag $TAG carries no src/cli_tools_core" >&2; exit 2; }

# The tree hash: sha256 over "<sha256 of file>  <relative path>\n" for every
# file, sorted by relative path. tests/test_core_copy.py computes the same.
TREE=$(python3 - "$SRC" <<'PY'
import hashlib, sys
from pathlib import Path
root = Path(sys.argv[1])
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
print(digest.hexdigest())
PY
)

printf 'tag %s\ncommit %s\ntree %s\n' "$TAG" "$SHA" "$TREE" > "$SRC/VERSION"
printf '%s\n' "Shared code, maintained outside this repository at the tag recorded in VERSION; do not edit here, run scripts/sync-core.sh instead." > "$SRC/README"

case "$DEST" in */_core) ;; *) echo "sync-core: refusing to replace $DEST" >&2; exit 70 ;; esac
rm -rf "$DEST"
mv "$SRC" "$DEST"
count=$(find "$DEST" -type f | wc -l | tr -d ' ')
echo "sync-core: $DEST is now $TAG at $SHA, tree $TREE, $count files"
