#!/usr/bin/env python3
"""Read-only regression checks against saved cross-harness run artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from cross_harness import runner
from cross_harness.summarize import parse_events


def _uncached_tracked_path(
    cwd: Path, value: str, _tracked_basenames: dict[str, set[str]]
) -> str | None:
    """Run the pre-P1 tracked-path lookup for a behavior comparison."""
    return _TRACKED_PATH(cwd, value)


_TRACKED_PATH = runner._tracked_path


def _p1() -> int:
    runs_root = Path.home() / ".local/state/cross-harness/runs"
    if not runs_root.is_dir():
        print(f"run directory not found: {runs_root}")
        return 1

    runs = sorted(path for path in runs_root.iterdir() if path.is_dir())
    if not runs:
        print(f"no run directories found: {runs_root}")
        return 1

    checked = 0
    skipped = 0
    mismatches: list[str] = []
    for run_dir in runs:
        summary_path = run_dir / "summary.json"
        events_path = run_dir / "events.jsonl"
        if not summary_path.is_file() or not events_path.is_file():
            skipped += 1
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            skipped += 1
            continue
        cwd_value = summary.get("cwd")
        cwd = Path(cwd_value) if isinstance(cwd_value, str) else None
        if cwd is None or not cwd.is_dir() or not (cwd / ".git").exists():
            skipped += 1
            continue

        executions = parse_events(events_path).get("executions", [])
        with patch.object(runner, "_tracked_path", _uncached_tracked_path):
            before = runner._self_reversions(cwd, executions)
        after = runner._self_reversions(cwd, executions)
        checked += 1
        if before != after:
            mismatches.append(run_dir.name)

    if mismatches:
        print("P1 self-reversion detection changed for: " + ", ".join(mismatches))
        return 1
    print(f"P1 passed: {checked} readable Git-backed runs matched; {skipped} skipped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit", required=True, choices=("p1",))
    args = parser.parse_args()
    return _p1() if args.unit == "p1" else 2


if __name__ == "__main__":
    raise SystemExit(main())
