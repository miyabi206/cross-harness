#!/usr/bin/env python3
"""Read-only regression checks against saved cross-harness run artifacts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
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


def _p3() -> int:
    """Exercise retry artifacts against Git and their saved retry permissions read-only."""
    runs_root = Path.home() / ".local/state/cross-harness/runs"
    checked = 0
    skipped = 0
    mismatches: list[str] = []
    if runs_root.is_dir():
        for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
            baseline_path = run_dir / "baseline.json"
            summary_path = run_dir / "summary.json"
            if not baseline_path.is_file() or not summary_path.is_file():
                skipped += 1
                continue
            try:
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                mismatches.append(run_dir.name)
                continue
            # Blocked runs never reached finalize_run's diff collection and
            # are intentionally fail-closed by the retry guard.
            if isinstance(summary, dict) and summary.get("status") == "blocked":
                skipped += 1
                continue
            for record in (baseline, summary):
                details = record.get("diff_summary") if isinstance(record, dict) else None
                if not isinstance(details, list):
                    mismatches.append(run_dir.name)
                    break
                if any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("file"), str)
                    or (
                        item.get("removed_preexisting_change") is not True
                        and not isinstance(item.get("fingerprint"), (str, type(None)))
                    )
                    for item in details
                ):
                    mismatches.append(run_dir.name)
                    break
            else:
                allowed, _ = runner._recorded_retry_changes(run_dir)
                if allowed is None:
                    if summary.get("diff_check") == "unavailable":
                        skipped += 1
                        continue
                    mismatches.append(run_dir.name)
                    continue
                changed = any(
                    item.get("removed_preexisting_change") is not True
                    for item in summary["diff_summary"]
                )
                if changed and not allowed:
                    mismatches.append(run_dir.name)
                    continue
                checked += 1
    else:
        skipped += 1

    tests = (
        "tests.test_runner.RunnerTests.test_retry_continues_failed_write_run_changes",
        "tests.test_runner.RunnerTests.test_retry_rejects_changes_not_left_by_failed_run",
        "tests.test_runner.RunnerTests.test_isolated_retry_reuses_failed_run_worktree",
        "tests.test_runner.RunnerTests.test_retry_chain_accepts_each_previous_run_delta",
    )
    environment = dict(os.environ)
    environment.pop("CROSS_HARNESS_ACTIVE", None)
    result = subprocess.run(
        [sys.executable, "-m", "unittest", *tests],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if mismatches:
        print("P3 malformed retry diff artifacts: " + ", ".join(sorted(set(mismatches))))
        return 1
    if result.returncode:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        return result.returncode
    print(f"P3 passed: {checked} saved baseline/summary pairs matched; {skipped} skipped; 4 Git retry scenarios passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit", required=True, choices=("p1", "p2", "p3"))
    args = parser.parse_args()
    if args.unit == "p1":
        return _p1()
    if args.unit == "p2":
        return _p2()
    return _p3()


if __name__ == "__main__":
    raise SystemExit(main())
