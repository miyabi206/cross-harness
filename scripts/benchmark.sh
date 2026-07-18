#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: $0 INPUT.json OUTPUT.md" >&2
  exit 2
fi

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"
./bin/cross-harness benchmark --input "$1" --output "$2"

