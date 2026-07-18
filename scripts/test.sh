#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

python3 -m compileall -q src
PYTHONPATH=src python3 -m unittest discover -s tests -v
./bin/cross-harness validate --config config/default.toml
git diff --check

