#!/usr/bin/env python3
"""Read-only regression checks against saved cross-harness run artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from cross_harness import runner
from cross_harness.summarize import load_final, parse_events


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


def _p2() -> int:
    runs_root = Path.home() / ".local/state/cross-harness/runs"
    target_name = "20260721T222034-9e55a8fd"
    if not runs_root.is_dir():
        print(f"run directory not found: {runs_root}")
        return 1

    target_found = False
    target_verified = False
    non_overage_rejected_runs: list[str] = []
    changed_decisions: list[str] = []
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        events_path = run_dir / "events.jsonl"
        if not events_path.is_file():
            continue
        parsed = parse_events(events_path)
        final = load_final(run_dir / "final.json") or {}
        completed = final.get("status") == "success"
        legacy_rate_limit_blocked = (
            parsed.get("blocked_category") == "rate_limit"
            or parsed.get("rate_limit_notice") == "overage_allowed"
        )
        rate_limit_blocked = (
            parsed.get("blocked_category") == "rate_limit"
            or (
                parsed.get("rate_limit_notice") == "overage_allowed"
                and not completed
            )
        )
        if legacy_rate_limit_blocked and not rate_limit_blocked:
            changed_decisions.append(run_dir.name)
        if parsed.get("blocked_category") == "rate_limit":
            non_overage_rejected_runs.append(run_dir.name)
        if run_dir.name == target_name:
            target_found = True
            target_verified = (
                completed
                and parsed.get("blocked_category") is None
                and parsed.get("rate_limit_notice") == "overage_allowed"
                and not rate_limit_blocked
            )

    print("P2 changed rate-limit decisions: " + (", ".join(changed_decisions) or "none"))
    if not target_found:
        print(f"P2 target run not found: {target_name}")
        return 1
    if not target_verified:
        print(f"P2 target run did not become overage_allowed: {target_name}")
        return 1
    if target_name not in changed_decisions:
        print(f"P2 target run was not listed as changed: {target_name}")
        return 1
    non_overage_text = ", ".join(non_overage_rejected_runs) or "none present"
    print(
        "P2 passed: target has overage_allowed notice; rejected non-overage runs remain blocked: "
        + non_overage_text
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit", required=True, choices=("p1", "p2"))
    args = parser.parse_args()
    return _p1() if args.unit == "p1" else _p2()


if __name__ == "__main__":
    raise SystemExit(main())
