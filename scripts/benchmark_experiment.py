#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cross_harness.benchmark import load_task_plan  # noqa: E402
from cross_harness.experiment import (  # noqa: E402
    collect_claude_metrics,
    collect_codex_metrics,
    first_check_pass,
    load_jsonl,
    render_task_prompt,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="run one isolated Claude/cross-harness benchmark")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--task-type", required=True)
    parser.add_argument("--configuration", choices=("baseline", "cross_harness"), required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, default=Path.home() / ".local/state/cross-harness/runs")
    parser.add_argument("--model", default="opus[1m]")
    parser.add_argument("--effort", default="xhigh")
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--check-timeout-seconds", type=int, default=3600)
    return parser.parse_args()


def git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def existing_runs(root: Path) -> set[Path]:
    if not root.exists():
        return set()
    return {path for path in root.iterdir() if path.is_dir()}


def run_claude(args: argparse.Namespace, prompt: str, stdout_path: Path, stderr_path: Path) -> tuple[int, float]:
    command = [
        "claude",
        "-p",
        "--model",
        args.model,
        "--effort",
        args.effort,
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read,Glob,Grep,Edit,Write,Bash,Task",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
    ]
    if args.configuration == "baseline":
        command.insert(1, "--safe-mode")
    command.append(prompt)
    environment = os.environ.copy()
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY"):
        environment.pop(name, None)
    started = time.monotonic()
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            result = subprocess.run(
                command,
                cwd=args.worktree,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                text=True,
                check=False,
                timeout=args.timeout_seconds,
            )
        return result.returncode, time.monotonic() - started
    except subprocess.TimeoutExpired:
        return 124, time.monotonic() - started


def run_checks(args: argparse.Namespace, checks: list[str]) -> list[dict]:
    check_dir = args.artifacts / "checks"
    check_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    results: list[dict] = []
    for index, command in enumerate(checks, 1):
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=args.worktree,
                env=environment,
                shell=True,
                executable="/bin/zsh",
                text=True,
                capture_output=True,
                check=False,
                timeout=args.check_timeout_seconds,
            )
            exit_code = result.returncode
            output = result.stdout + result.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            output = (exc.stdout or "") + (exc.stderr or "")
        duration = time.monotonic() - started
        (check_dir / f"{index:02d}.log").write_text(output, encoding="utf-8")
        results.append({"command": command, "exit_code": exit_code, "duration_seconds": round(duration, 3)})
    return results


def main() -> int:
    args = parse_args()
    tasks = load_task_plan(args.tasks)
    task = next((item for item in tasks if item["task_type"] == args.task_type), None)
    if task is None:
        raise SystemExit(f"unknown task type: {args.task_type}")
    args.worktree = args.worktree.resolve()
    args.artifacts = args.artifacts.resolve()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    head = git(args.worktree, "rev-parse", "HEAD")
    if head.returncode or head.stdout.strip() != task["start_commit"]:
        raise SystemExit(f"worktree HEAD does not match start commit: {head.stdout.strip()}")
    status = git(args.worktree, "status", "--short")
    if status.returncode or status.stdout.strip():
        raise SystemExit(f"worktree must be clean before benchmark:\n{status.stdout}")

    prompt = render_task_prompt(task)
    (args.artifacts / "prompt.md").write_text(prompt, encoding="utf-8")
    before = existing_runs(args.runtime_root)
    returncode, duration = run_claude(
        args,
        prompt,
        args.artifacts / "claude.jsonl",
        args.artifacts / "claude.stderr.log",
    )
    after = existing_runs(args.runtime_root)
    new_runs = sorted(after - before)
    matching_runs: list[Path] = []
    for run_dir in new_runs:
        state_file = run_dir / "state.json"
        summary_file = run_dir / "summary.json"
        if not state_file.exists() or not summary_file.exists():
            continue
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if Path(str(state.get("cwd", ""))).resolve() == args.worktree:
            matching_runs.append(run_dir)

    check_results = run_checks(args, task["checks"])
    events = load_jsonl(args.artifacts / "claude.jsonl")
    claude = collect_claude_metrics(events)
    codex = collect_codex_metrics(matching_runs)
    final_pass = all(item["exit_code"] == 0 for item in check_results)

    if args.configuration == "baseline":
        raw_terminal_bytes = claude.terminal_bytes
        summary_bytes = claude.terminal_bytes
    else:
        delegated_summary = sum(
            len((run_dir / "summary.txt").read_bytes())
            for run_dir in matching_runs
            if (run_dir / "summary.txt").exists()
        )
        native_terminal = max(0, claude.terminal_bytes - delegated_summary)
        raw_terminal_bytes = native_terminal + codex.raw_terminal_bytes
        summary_bytes = native_terminal + codex.summary_bytes

    record = {
        "task_type": task["task_type"],
        "configuration": args.configuration,
        "source": task["issue"],
        "claude_usage": claude.usage,
        "codex_usage": codex.usage,
        "message_bytes": claude.message_bytes + codex.message_bytes,
        "raw_terminal_bytes": raw_terminal_bytes,
        "summary_bytes": summary_bytes,
        "files_read": claude.read_operations + codex.read_operations,
        "subagents": claude.subagents + len(matching_runs),
        "retries": codex.retries,
        "duration_seconds": round(duration, 3),
        "first_check_pass": first_check_pass(task["checks"], claude.commands, codex.commands),
        "final_check_pass": final_pass,
        "human_corrections": 0,
        "task_success": returncode == 0 and claude.result_success and final_pass,
    }
    audit = {
        "claude_exit_code": returncode,
        "claude_result_success": claude.result_success,
        "claude_rate_limit_events": claude.rate_limit_events,
        "claude_read_operations": claude.read_operations,
        "claude_read_targets": list(claude.read_targets),
        "codex_read_operations": codex.read_operations,
        "codex_run_dirs": [str(path) for path in matching_runs],
        "check_results": check_results,
        "git_status": git(args.worktree, "status", "--short").stdout.splitlines(),
        "git_diff_stat": git(args.worktree, "diff", "--stat").stdout.splitlines(),
        "automatic_task_success": record["task_success"],
        "manual_done_when_audit": "pending",
    }
    (args.artifacts / "record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.artifacts / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(record, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
