from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import json
import re


READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead"}
READ_COMMAND = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:rg|grep|sed|cat|head|tail|find|ls|wc|git\s+(?:show|diff|log|status))\b"
)
EXIT_CODE = re.compile(r"(?:^|\n)Exit code\s+(\d+)(?:\n|$)")
RUN_DIR = re.compile(r"run_dir:\s*(/[^\s]+/runs/[^\s]+)")


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int


@dataclass(frozen=True)
class ClaudeMetrics:
    usage: int
    message_bytes: int
    terminal_bytes: int
    read_operations: int
    read_targets: tuple[str, ...]
    subagents: int
    commands: tuple[CommandResult, ...]
    run_dirs: tuple[str, ...]
    result_success: bool
    rate_limit_events: int


@dataclass(frozen=True)
class CodexMetrics:
    usage: int
    message_bytes: int
    raw_terminal_bytes: int
    summary_bytes: int
    read_operations: int
    retries: int
    commands: tuple[CommandResult, ...]


def load_jsonl(path: Path) -> list[dict]:
    events: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{number}: {exc}") from exc
        if isinstance(event, dict):
            events.append(event)
    return events


def _utf8_size(value: object) -> int:
    if value is None:
        return 0
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return len(value.encode("utf-8"))


def _claude_exit_code(content: object, is_error: bool) -> int:
    if isinstance(content, str):
        match = EXIT_CODE.search(content)
        if match:
            return int(match.group(1))
    return 1 if is_error else 0


def collect_claude_metrics(events: Iterable[dict]) -> ClaudeMetrics:
    tool_commands: dict[str, str] = {}
    commands: list[CommandResult] = []
    read_targets: set[str] = set()
    read_operations = 0
    subagents = 0
    terminal_bytes = 0
    message_bytes = 0
    run_dirs: set[str] = set()
    result_event: dict | None = None
    rate_limit_events = 0

    for event in events:
        event_type = event.get("type")
        if event_type == "rate_limit_event":
            rate_limit_events += 1
        if event_type == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            for block in message.get("content", []):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    message_bytes += _utf8_size(block.get("text", ""))
                elif block_type == "tool_use":
                    name = block.get("name")
                    tool_id = block.get("id")
                    inputs = block.get("input") if isinstance(block.get("input"), dict) else {}
                    if name == "Bash" and isinstance(tool_id, str):
                        tool_commands[tool_id] = str(inputs.get("command", ""))
                    if name in READ_TOOLS:
                        read_operations += 1
                        for key in ("file_path", "path", "pattern"):
                            value = inputs.get(key)
                            if isinstance(value, str) and value:
                                read_targets.add(value)
                    if name in {"Task", "Agent"}:
                        subagents += 1
        elif event_type == "user":
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            for block in message.get("content", []):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id")
                command = tool_commands.get(str(tool_id))
                if command is None:
                    continue
                content = block.get("content", "")
                result = event.get("tool_use_result")
                if isinstance(result, dict):
                    output_size = _utf8_size(result.get("stdout", "")) + _utf8_size(result.get("stderr", ""))
                else:
                    output_size = _utf8_size(content)
                terminal_bytes += output_size
                for match in RUN_DIR.finditer(str(content)):
                    run_dirs.add(match.group(1))
                commands.append(
                    CommandResult(
                        command=command,
                        exit_code=_claude_exit_code(content, bool(block.get("is_error"))),
                    )
                )
        elif event_type == "result":
            result_event = event

    usage = 0
    success = False
    if result_event is not None:
        usage_data = result_event.get("usage") if isinstance(result_event.get("usage"), dict) else {}
        usage = sum(
            int(usage_data.get(key, 0) or 0)
            for key in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            )
        )
        success = result_event.get("subtype") == "success" and not bool(result_event.get("is_error"))

    return ClaudeMetrics(
        usage=usage,
        message_bytes=message_bytes,
        terminal_bytes=terminal_bytes,
        read_operations=read_operations,
        read_targets=tuple(sorted(read_targets)),
        subagents=subagents,
        commands=tuple(commands),
        run_dirs=tuple(sorted(run_dirs)),
        result_success=success,
        rate_limit_events=rate_limit_events,
    )


def collect_codex_metrics(run_dirs: Iterable[Path]) -> CodexMetrics:
    usage = 0
    message_bytes = 0
    raw_terminal_bytes = 0
    summary_bytes = 0
    read_operations = 0
    retries = 0
    commands: list[CommandResult] = []

    for run_dir in run_dirs:
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        raw_terminal_bytes += int(summary.get("raw_artifact_bytes", 0) or 0)
        summary_bytes += int(summary.get("summary_bytes", 0) or 0)
        run_usage = summary.get("usage") if isinstance(summary.get("usage"), dict) else {}
        usage += int(run_usage.get("input_tokens", 0) or 0) + int(run_usage.get("output_tokens", 0) or 0)
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        retries += max(0, int(state.get("attempts", 1) or 1) - 1)
        for event in load_jsonl(run_dir / "events.jsonl"):
            if event.get("type") != "item.completed":
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") == "agent_message":
                message_bytes += _utf8_size(item.get("text", ""))
            elif item.get("type") == "command_execution":
                command = str(item.get("command", ""))
                if READ_COMMAND.search(command):
                    read_operations += 1
                commands.append(CommandResult(command=command, exit_code=int(item.get("exit_code", 1) or 0)))

    return CodexMetrics(
        usage=usage,
        message_bytes=message_bytes,
        raw_terminal_bytes=raw_terminal_bytes,
        summary_bytes=summary_bytes,
        read_operations=read_operations,
        retries=retries,
        commands=tuple(commands),
    )


def command_matches_check(command: str, check: str) -> bool:
    normalized_command = " ".join(command.split())
    normalized_check = " ".join(check.split())
    if normalized_check in normalized_command:
        return True
    tail = normalized_check.split("&&", 1)[-1].strip()
    return len(tail) >= 12 and tail in normalized_command


def first_check_pass(
    checks: Iterable[str],
    claude_commands: Iterable[CommandResult],
    codex_commands: Iterable[CommandResult] = (),
) -> bool:
    checks = tuple(checks)
    for result in (*tuple(codex_commands), *tuple(claude_commands)):
        if any(command_matches_check(result.command, check) for check in checks):
            return result.exit_code == 0
    return False


def render_task_prompt(task: dict) -> str:
    done = "\n".join(f"- {item}" for item in task["done_when"])
    checks = "\n".join(f"- `{item}`" for item in task["checks"])
    return f"""ベンチマーク用の実装タスクです。次の完了済みissueを、現在の開始commitから再実装してください。

source: {task['issue']}
goal: {task['goal']}

done_when:
{done}

required_checks:
{checks}

作業条件:
- 最初にrepository rootのAGENTS.mdを読み、適用される規約に従う。
- このディレクトリは既にベンチマーク専用worktreeとして隔離済み。新しいworktree/branchの作成、commit、push、PR作成は行わない。
- source PR、完成後commit、別構成のworktreeや出力を参照せず、この開始状態と要件だけから実装する。
- `.env`、credential、secretを読まず、production、外部API、cloudへ接続しない。
- TEST_DATABASE_URLがある場合は隔離済みベンチマークDBなので利用してよいが、Dockerの起動・停止やDB/volume削除は行わない。
- 実装後にrequired_checksを実行する。実行不能または失敗は成功扱いにしない。
- 最終報告は日本語で、変更ファイル、check結果、残課題を簡潔に示す。
"""

