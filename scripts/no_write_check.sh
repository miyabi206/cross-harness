#!/bin/bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/cross-harness-no-write.XXXXXX")
worktree="$tmpdir/repository"

cleanup() {
    rm -rf "$tmpdir"
}
trap cleanup EXIT HUP INT TERM

mkdir "$worktree"
git -C "$repo_root" ls-files -co --exclude-standard -z > "$tmpdir/files"
while IFS= read -r -d '' path; do
    destination="$worktree/$path"
    mkdir -p "$(dirname "$destination")"
    cp -pP "$repo_root/$path" "$destination"
done < "$tmpdir/files"

git -C "$worktree" init -q
git -C "$worktree" add -A
git -C "$worktree" -c user.name='no-write check' -c user.email='no-write-check@example.invalid' commit -qm 'baseline'

find "$worktree" -path "$worktree/.git" -prune -o \( -type f -o -type l \) -print > "$tmpdir/before"
LC_ALL=C sort "$tmpdir/before" -o "$tmpdir/before"

env -u CROSS_HARNESS_ACTIVE "$worktree/scripts/test.sh"

find "$worktree" -path "$worktree/.git" -prune -o \( -type f -o -type l \) -print > "$tmpdir/after"
LC_ALL=C sort "$tmpdir/after" -o "$tmpdir/after"
new_files="$tmpdir/new-files"
comm -13 "$tmpdir/before" "$tmpdir/after" > "$new_files"

if [ -s "$new_files" ]; then
    echo "scripts/test.sh created files:" >&2
    cat "$new_files" >&2
    exit 1
fi
