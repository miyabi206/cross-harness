#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

export PYTHONDONTWRITEBYTECODE=1

python3 - <<'PY'
from pathlib import Path
import sys

failed = False
for path in sorted(Path("src").rglob("*.py")):
    if "__pycache__" in path.parts:
        continue
    try:
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")
    except SyntaxError as error:
        print(f"{path}:{error.lineno}: {error.msg}", file=sys.stderr)
        failed = True

if failed:
    raise SystemExit(1)
PY
PYTHONPATH=src python3 -m unittest discover -s tests -v
./bin/cross-harness validate --config config/default.toml
git diff --check
