from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import json
import re
import subprocess

from .errors import HarnessError


TASK_TYPES = {"small_fix", "medium_feature", "test_addition", "bug_debug", "cross_refactor"}
CONFIGURATIONS = {"baseline", "cross_harness"}
METRICS = {
    "claude_usage",
    "codex_usage",
    "message_bytes",
    "raw_terminal_bytes",
    "summary_bytes",
    "files_read",
    "subagents",
    "retries",
    "duration_seconds",
    "first_check_pass",
    "final_check_pass",
    "human_corrections",
    "task_success",
}
INTEGER_METRICS = {
    "claude_usage",
    "codex_usage",
    "message_bytes",
    "raw_terminal_bytes",
    "summary_bytes",
    "files_read",
    "subagents",
    "retries",
    "human_corrections",
}
BOOLEAN_METRICS = {"first_check_pass", "final_check_pass", "task_success"}
REQUIRED_FIELDS = {"task_type", "configuration", "source", "duration_seconds", *METRICS}
PLACEHOLDER_SOURCE = "replace with reproducible issue"
TASK_PLAN_FIELDS = {"task_type", "issue", "start_commit", "goal", "done_when", "checks"}
PLACEHOLDER_ISSUE = "replace with completed issue url"
FULL_COMMIT = re.compile(r"[0-9a-f]{40}")


def load_records(path: Path, *, allow_template: bool = False) -> list[dict]:
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"invalid benchmark input: {exc}") from exc
    if not isinstance(records, list):
        raise HarnessError("benchmark input must be a JSON array")
    seen: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            raise HarnessError("each benchmark record must be an object")
        task = record.get("task_type")
        configuration = record.get("configuration")
        if task not in TASK_TYPES or configuration not in CONFIGURATIONS:
            raise HarnessError(f"unknown task/configuration pair: {task}/{configuration}")
        if (task, configuration) in seen:
            raise HarnessError(f"duplicate task/configuration pair: {task}/{configuration}")
        seen.add((task, configuration))
        unknown = set(record) - REQUIRED_FIELDS
        if unknown:
            raise HarnessError("unknown benchmark metrics: " + ", ".join(sorted(unknown)))
        missing_fields = REQUIRED_FIELDS - set(record)
        if missing_fields:
            raise HarnessError(
                f"missing benchmark metrics for {task}/{configuration}: "
                + ", ".join(sorted(missing_fields))
            )
        source = record["source"]
        if not isinstance(source, str) or not source.strip():
            raise HarnessError(f"source must be a non-empty string for {task}/{configuration}")
        if not allow_template and source.strip().lower() == PLACEHOLDER_SOURCE:
            raise HarnessError(f"benchmark source is still a placeholder for {task}/{configuration}")
        for metric in INTEGER_METRICS:
            value = record[metric]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HarnessError(f"{metric} must be a non-negative integer for {task}/{configuration}")
        duration = record["duration_seconds"]
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
            raise HarnessError(f"duration_seconds must be non-negative for {task}/{configuration}")
        for metric in BOOLEAN_METRICS:
            if not isinstance(record[metric], bool):
                raise HarnessError(f"{metric} must be boolean for {task}/{configuration}")
        if not allow_template:
            for metric in ("message_bytes", "raw_terminal_bytes", "duration_seconds"):
                if record[metric] <= 0:
                    raise HarnessError(f"{metric} must be positive for measured {task}/{configuration}")
    missing = {(task, configuration) for task in TASK_TYPES for configuration in CONFIGURATIONS} - seen
    if missing:
        raise HarnessError("missing benchmark pairs: " + ", ".join(f"{task}/{config}" for task, config in sorted(missing)))
    for task in TASK_TYPES:
        sources = {
            record["source"]
            for record in records
            if record["task_type"] == task
        }
        if len(sources) != 1:
            raise HarnessError(f"baseline and cross_harness must use the same source for {task}")
    return records


def load_task_plan(path: Path, *, allow_template: bool = False) -> list[dict]:
    try:
        tasks = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"invalid benchmark task plan: {exc}") from exc
    if not isinstance(tasks, list):
        raise HarnessError("benchmark task plan must be a JSON array")
    seen: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise HarnessError("each benchmark task must be an object")
        unknown = set(task) - TASK_PLAN_FIELDS
        missing = TASK_PLAN_FIELDS - set(task)
        if unknown:
            raise HarnessError("unknown benchmark task fields: " + ", ".join(sorted(unknown)))
        if missing:
            raise HarnessError("missing benchmark task fields: " + ", ".join(sorted(missing)))
        task_type = task["task_type"]
        if task_type not in TASK_TYPES:
            raise HarnessError(f"unknown benchmark task type: {task_type}")
        if task_type in seen:
            raise HarnessError(f"duplicate benchmark task type: {task_type}")
        seen.add(task_type)
        for field in ("issue", "start_commit", "goal"):
            if not isinstance(task[field], str) or not task[field].strip():
                raise HarnessError(f"{field} must be a non-empty string for {task_type}")
        if not allow_template:
            if task["issue"].strip().lower() == PLACEHOLDER_ISSUE:
                raise HarnessError(f"benchmark issue is still a placeholder for {task_type}")
            if not FULL_COMMIT.fullmatch(task["start_commit"].strip().lower()):
                raise HarnessError(f"start_commit must be a full 40-character commit for {task_type}")
        for field, low, high in (("done_when", 1, 3), ("checks", 1, 20)):
            values = task[field]
            if (
                not isinstance(values, list)
                or not low <= len(values) <= high
                or any(not isinstance(value, str) or not value.strip() for value in values)
                or len(values) != len(set(values))
            ):
                raise HarnessError(f"{field} must contain {low}..{high} unique non-empty strings for {task_type}")
    missing_types = TASK_TYPES - seen
    if missing_types:
        raise HarnessError("missing benchmark task types: " + ", ".join(sorted(missing_types)))
    return tasks


def verify_task_commits(tasks: list[dict], repository: Path) -> list[str]:
    root = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if root.returncode:
        raise HarnessError(f"benchmark repository is not a Git checkout: {repository}")
    verified: list[str] = []
    for task in tasks:
        commit = task["start_commit"]
        result = subprocess.run(
            ["git", "-C", str(repository), "cat-file", "-e", f"{commit}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise HarnessError(f"start commit not found for {task['task_type']}: {commit}")
        verified.append(f"{task['task_type']}: {commit}")
    return verified


def render(records: list[dict]) -> str:
    by_pair = {(record["task_type"], record["configuration"]): record for record in records}
    lines = [
        "# Benchmark results",
        "",
        "## Sources",
        "",
        "| Task | Baseline | Cross harness |",
        "|---|---|---|",
    ]
    for task in sorted(TASK_TYPES):
        baseline = by_pair[(task, "baseline")]
        cross = by_pair[(task, "cross_harness")]
        lines.append(f"| {task} | {baseline['source']} | {cross['source']} |")
    lines.extend([
        "",
        "## Usage and output",
        "",
        "| Task | Configuration | Claude usage | Codex usage | Message bytes | Raw terminal bytes | Summary bytes | Compression | Files read | Subagents |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for task in sorted(TASK_TYPES):
        for configuration in ("baseline", "cross_harness"):
            item = by_pair[(task, configuration)]
            raw = int(item.get("raw_terminal_bytes", 0))
            summary = int(item.get("summary_bytes", 0))
            compression = (1 - summary / raw) * 100 if raw else 0
            lines.append(
                f"| {task} | {configuration} | {item['claude_usage']} | {item['codex_usage']} | "
                f"{item['message_bytes']} | {raw} | {summary} | {compression:.1f}% | "
                f"{item['files_read']} | {item['subagents']} |"
            )
    lines.extend([
        "",
        "## Execution and quality",
        "",
        "| Task | Configuration | Duration (s) | Retries | First check | Final check | Human corrections | Success |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    for task in sorted(TASK_TYPES):
        for configuration in ("baseline", "cross_harness"):
            item = by_pair[(task, configuration)]
            lines.append(
                f"| {task} | {configuration} | {item['duration_seconds']} | {item['retries']} | "
                f"{item['first_check_pass']} | {item['final_check_pass']} | "
                f"{item['human_corrections']} | {item['task_success']} |"
            )
    cross = [item for item in records if item["configuration"] == "cross_harness"]
    ratios = [1 - int(item.get("summary_bytes", 0)) / int(item["raw_terminal_bytes"]) for item in cross if int(item.get("raw_terminal_bytes", 0))]
    average = sum(ratios) / len(ratios) * 100 if ratios else 0
    failures = [item["task_type"] for item in cross if not item.get("task_success")]
    first_passes = sum(bool(item["first_check_pass"]) for item in cross)
    final_passes = sum(bool(item["final_check_pass"]) for item in cross)
    mean_retries = sum(int(item["retries"]) for item in cross) / len(cross)
    baseline_duration = sum(float(item["duration_seconds"]) for item in records if item["configuration"] == "baseline")
    cross_duration = sum(float(item["duration_seconds"]) for item in cross)
    duration_ratio = cross_duration / baseline_duration if baseline_duration else 0
    lines.extend([
        "",
        "## Initial routing recommendation",
        "",
        f"- Mean terminal compression: {average:.1f}% (target: at least 90%).",
        f"- First/final check passes: {first_passes}/5 and {final_passes}/5.",
        f"- Mean cross-harness retries: {mean_retries:.2f}.",
        f"- Cross/baseline total duration ratio: {duration_ratio:.2f}.",
        f"- Cross-harness task failures: {', '.join(failures) if failures else 'none'}.",
    ])
    if average < 90:
        lines.append("- Tighten command-specific quiet flags and failure extraction before lowering model effort.")
    elif failures or final_passes < len(cross):
        lines.append("- Keep current model/effort routing until every task passes its final check.")
    else:
        lines.append("- Keep the current output limits; review per-role effort only after two weeks of observed success data.")
    lines.append("- Do not change subscription models automatically; apply routing changes through personal configuration review.")
    lines.append("")
    return "\n".join(lines)
