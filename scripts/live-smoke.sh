#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
smoke_root=$(mktemp -d "${TMPDIR:-/tmp}/cross-harness-smoke.XXXXXX")
trap 'rm -rf -- "$smoke_root"' EXIT HUP INT TERM

git -C "$smoke_root" init -q
git -C "$smoke_root" config user.email smoke@example.invalid
git -C "$smoke_root" config user.name 'Cross Harness Smoke'
echo 'smoke fixture' > "$smoke_root/README.md"
git -C "$smoke_root" add README.md
git -C "$smoke_root" commit -qm initial

task_file="$smoke_root.task.md"
trap 'rm -rf -- "$smoke_root"; rm -f -- "$task_file"' EXIT HUP INT TERM
{
  echo '# Goal'
  echo 'Read README.md without changing files and report success.'
  echo '# Done when'
  echo '- README.md content is reported.'
  echo '- No files are changed.'
} > "$task_file"

"${HOME}/.local/bin/cross-harness" delegate \
  --role tester \
  --kind test \
  --task-file "$task_file" \
  --cwd "$smoke_root"

