#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

# Keep tests isolated from any harness invocation environment.
for variable in $(awk 'BEGIN { for (name in ENVIRON) if (name ~ /^CROSS_HARNESS_/) print name }'); do
    unset "$variable"
done

if [ -x .venv/bin/python ]; then
    python=.venv/bin/python
else
    python=python3
fi

if ! "$python" -c 'import pytest' >/dev/null 2>&1; then
    echo "pytest is required to run tests. Install it with either:" >&2
    echo "  - uv: uv sync --group dev" >&2
    echo "  - without uv: python3 -m venv .venv && .venv/bin/python -m pip install pytest" >&2
    exit 1
fi

if [ "$python" = .venv/bin/python ] && command -v uv >/dev/null 2>&1; then
    if ! uv_output=$(uv sync --check 2>&1); then
        printf '%s\n' "$uv_output" >&2
        echo "uv could not verify that .venv is in sync with uv.lock; the lock may be out of sync or uv may not have completed the check. Run:" >&2
        echo "  uv sync --group dev" >&2
        exit 1
    fi
fi

export PYTHONDONTWRITEBYTECODE=1

"$python" - <<'PY'
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
PYTHONPATH=src "$python" -m pytest tests -v
./bin/cross-harness validate --config config/default.toml
git diff --check
