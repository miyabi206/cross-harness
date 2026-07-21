from __future__ import annotations

from pathlib import Path
import hashlib
import json
import re


FAILURE_WORDS = re.compile(r"\b(error|failed|failure|exception|panic|traceback)\b", re.IGNORECASE)
VOLATILE = re.compile(r"\b(?:0x[0-9a-f]+|\d+(?:\.\d+)?(?:ms|s)?|[0-9a-f]{8,})\b", re.IGNORECASE)
_CLAUDE_RATE_LIMIT_CODES = frozenset({
    "rate_limit",
    "rate_limit_error",
    "rate_limit_exceeded",
    "usage_limit",
    "quota_exceeded",
})
_CLAUDE_AUTHENTICATION_CODES = frozenset({
    "authentication_error",
    "authentication_failed",
    "auth_error",
    "auth_failure",
    "unauthorized",
    "not_authenticated",
    "login_required",
    "oauth_org_not_allowed",
})
_READ_COMMANDS = frozenset({
    "cat", "less", "more", "head", "tail", "bat", "echo", "printf", "grep", "rg", "ag", "sed",
    "awk", "wc", "file", "stat", "ls", "find",
})
_SHELL_WRAPPER = re.compile(r"^/bin/(?:zsh|bash|sh)\s+-lc\s+(['\"])(.*)\1$", re.DOTALL)
_COMMAND_SEPARATOR = re.compile(r"&&|\|\||;|\||\n")


def _command_segments(command: str) -> list[str]:
    """Split a Codex shell command into the command lines it executes."""
    match = _SHELL_WRAPPER.match(command.strip())
    inner = match.group(2) if match else command
    return _COMMAND_SEPARATOR.split(inner)


def _starts_with_read_command(command: str) -> bool:
    token = command.lstrip().split(None, 1)
    return bool(token) and token[0] in _READ_COMMANDS


def command_matches_check(command: str, check: str) -> bool:
    normalized_command = " ".join(command.split())
    normalized_check = " ".join(check.split())
    tail = normalized_check.split("&&", 1)[-1].strip()
    matches = normalized_check in normalized_command or (len(tail) >= 12 and tail in normalized_command)
    if not matches:
        return False
    check_starts_with_read = _starts_with_read_command(normalized_check)
    for segment in _command_segments(command):
        normalized_segment = " ".join(segment.split())
        if normalized_check not in normalized_segment and (len(tail) < 12 or tail not in normalized_segment):
            continue
        if not _starts_with_read_command(segment) or check_starts_with_read:
            return True
    return False


def _is_cross_harness_policy_denial(output: str) -> bool:
    """Return whether a Bash failure was rejected by this harness's hook."""
    return bool(re.match(
        r"^PreToolUse:Bash hook error: \[[^\r\n\]]*/cross-harness hook "
        r"claude-pre-tool-use\]: cross-harness: \S",
        output,
    ))


def parse_events(path: Path) -> dict:
    result = {
        "thread_id": None, "usage": {}, "errors": [], "commands": [],
        "executions": [], "blocked_category": None, "rate_limit_notice": None,
    }
    claude_commands: dict[str, str] = {}
    if not path.exists():
        return result
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                if FAILURE_WORDS.search(line):
                    result["errors"].append(line.strip())
                continue
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                result["thread_id"] = session_id
            kind = event.get("type")
            claude_blocked_category = _claude_blocked_category(event)
            if claude_blocked_category == "rate_limit":
                # A rate limit always wins if an event stream contains multiple errors.
                result["blocked_category"] = claude_blocked_category
            elif claude_blocked_category == "overage_allowed":
                result["rate_limit_notice"] = "overage_allowed"
            elif claude_blocked_category and result["blocked_category"] is None:
                result["blocked_category"] = claude_blocked_category
            if kind == "thread.started":
                result["thread_id"] = event.get("thread_id")
            elif kind == "turn.completed":
                result["usage"] = event.get("usage", {})
            elif kind == "result":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    result["usage"] = usage
                if event.get("is_error") is True:
                    result["errors"].append(_event_text(event))
            elif kind in {"turn.failed", "error"}:
                result["errors"].append(_event_text(event))
            _parse_claude_tool_events(event, claude_commands, result)
            item = event.get("item")
            # Codex emits an ``item.started`` event before the terminal
            # ``item.completed`` event.  The former has status=in_progress
            # and no exit code, so it must not be treated as a failure.
            if kind == "item.completed" and isinstance(item, dict) and item.get("type") == "command_execution":
                full_output = str(item.get("aggregated_output", item.get("output", "")))
                execution = {
                    "command": item.get("command", ""),
                    "exit_code": item.get("exit_code"),
                }
                if _is_cross_harness_policy_denial(full_output):
                    execution["policy_denied"] = True
                result["executions"].append(execution)
                if item.get("status") not in {None, "completed", "success"} or item.get("exit_code") not in {None, 0}:
                    output = full_output[-4000:]
                    command = {
                        "command": item.get("command", ""),
                        "exit_code": item.get("exit_code"),
                        "output": output,
                    }
                    if _is_cross_harness_policy_denial(full_output):
                        command["policy_denied"] = True
                    result["commands"].append(command)
    result["errors"] = [text for text in result["errors"] if text]
    return result


def _parse_claude_tool_events(event: dict, commands: dict[str, str], result: dict) -> None:
    """Record Claude Bash executions and retain failed results as commands."""
    message = event.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    if event.get("type") == "assistant":
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != "Bash" or not isinstance(block.get("id"), str):
                continue
            inputs = block.get("input")
            if isinstance(inputs, dict) and isinstance(inputs.get("command"), str):
                commands[block["id"]] = inputs["command"]
        return
    if event.get("type") != "user":
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        text = _tool_result_text(block)
        tool_id = block.get("tool_use_id")
        command = commands.get(tool_id) if isinstance(tool_id, str) else None
        if command is None:
            continue
        exit_code = 1 if block.get("is_error") is True else 0
        execution = {"command": command, "exit_code": exit_code}
        if _is_cross_harness_policy_denial(text):
            execution["policy_denied"] = True
        result["executions"].append(execution)
        if exit_code != 0:
            command_result = {"command": command, "exit_code": exit_code, "output": text[-4000:]}
            if execution.get("policy_denied"):
                command_result["policy_denied"] = True
            result["commands"].append(command_result)


def _tool_result_text(block: dict) -> str:
    content = block.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        values = [item.get("text", "") for item in content if isinstance(item, dict)]
        text = "\n".join(value for value in values if isinstance(value, str))
        if text:
            return text
    return ""


def _claude_blocked_category(event: dict) -> str | None:
    """Extract terminal Claude failures or an overage-allowed rate-limit notice.

    Claude emits rate limits as a dedicated event and reports error categories
    on ``result`` and ``system/api_retry`` events. Deliberately do not inspect
    free-form messages here: the runner's legacy text patterns are retained for
    Codex event streams only.
    """
    event_type = event.get("type")
    if event_type == "rate_limit_event":
        info = event.get("rate_limit_info")
        if not isinstance(info, dict) or info.get("status") != "rejected":
            return None
        if info.get("overageStatus") == "allowed" and info.get("isUsingOverage") is True:
            return "overage_allowed"
        return "rate_limit"
    if event_type == "system" and event.get("subtype") == "api_retry":
        return _claude_error_category(event.get("error"))
    if event_type != "result":
        return None
    for code in _claude_result_codes(event):
        category = _claude_error_category(code)
        if category:
            return category
    return None


def _claude_result_codes(event: dict) -> list[str]:
    """Return only structured Claude result error identifiers, never text."""
    codes: list[str] = []
    values: list[dict] = [event]
    for key in ("error", "error_info"):
        value = event.get(key)
        if isinstance(value, dict):
            values.append(value)
    for value in values:
        for key in ("subtype", "error_type", "error_code", "code", "error"):
            code = value.get(key)
            if isinstance(code, str):
                codes.append(code.lower().replace("-", "_").replace(" ", "_"))
    return codes


def _claude_error_category(code: object) -> str | None:
    if not isinstance(code, str):
        return None
    normalized = code.lower().replace("-", "_").replace(" ", "_")
    if normalized in _CLAUDE_RATE_LIMIT_CODES:
        return "rate_limit"
    if normalized in _CLAUDE_AUTHENTICATION_CODES:
        return "authentication"
    return None


def _event_text(event: dict) -> str:
    for key in ("message", "error", "detail", "result"):
        value = event.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return json.dumps(event, ensure_ascii=False, sort_keys=True)


def load_final(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def failure_signature(exit_code: int, parsed: dict, stderr: str = "") -> str | None:
    if exit_code == 0 and not parsed.get("errors") and not parsed.get("commands"):
        return None
    parts = [f"exit={exit_code}"]
    parts.extend(parsed.get("errors", [])[-3:])
    for command in parsed.get("commands", [])[-2:]:
        parts.extend([str(command.get("exit_code")), command.get("command", ""), command.get("output", "")])
    if stderr:
        parts.append(stderr[-3000:])
    normalized = VOLATILE.sub("<volatile>", "\n".join(parts)).lower()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def summary_item_text(value: object) -> str:
    """Return a deterministic, readable representation for summary list items."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def render_summary(summary: dict, limit: int) -> str:
    checks = summary.get("checks", [])
    if not checks:
        checks_text = "none declared"
    else:
        checks_text = "; ".join(
            f"{summary_item_text(item.get('check', item))}: {item.get('status', 'unknown')}"
            if isinstance(item, dict) else summary_item_text(item)
            for item in checks
        )
    lines = [
        f"status: {summary['status']}",
        f"run_dir: {summary['run_dir']}",
        f"exit_code: {summary['exit_code']}",
        f"role: {summary['role']}",
        f"model: {summary['model']}",
        f"effort: {summary['effort']}",
        f"changed_files: {', '.join(summary_item_text(item) for item in summary.get('changed_files', [])) or 'none'}",
        f"reported_changed_files: {', '.join(summary_item_text(item) for item in summary.get('reported_changed_files', [])) or 'none'}",
        f"tests (executor-reported): {'; '.join(summary_item_text(item) for item in summary.get('tests', [])) or 'not reported'}",
        f"checks: {checks_text}",
        f"unrelated_failed_commands: {summary.get('unrelated_failed_command_count', 0)}",
    ]
    unverified_changed_files = summary.get("unverified_changed_files", [])
    if unverified_changed_files:
        lines.append(
            "unverified_changed_files: "
            + ", ".join(summary_item_text(item) for item in unverified_changed_files)
        )
    if summary.get("work_completed"):
        lines.append(f"work_completed (executor-reported): {summary['work_completed']}")
    for reversion in summary.get("self_reversions", []):
        if isinstance(reversion, dict):
            lines.append(
                f"self_reversion: {reversion.get('target', 'unknown')} "
                f"({reversion.get('source', 'git')})"
            )
    if summary.get("self_reversion_check") == "unavailable":
        lines.append("self_reversion_check: unavailable")
    if summary.get("diff_check") == "unavailable":
        lines.append("diff_check: unavailable")
    if summary.get("rate_limit_notice") == "overage_allowed":
        lines.append("rate_limit_notice: overage_allowed")
    for item in summary.get("diff_summary", []):
        if item.get("removed_preexisting_change"):
            lines.append(f"diff: {item['file']} (pre-existing change removed during run)")
        elif item.get("untracked"):
            lines.append(f"diff: {item['file']} (untracked, {item.get('bytes', 0)} bytes)")
        else:
            lines.append(f"diff: {item['file']} (+{item.get('added', '?')}/-{item.get('deleted', '?')})")
    if summary.get("error"):
        lines.append(f"error: {summary['error']}")
    if summary.get("next_decision"):
        lines.append(f"next_decision: {summary['next_decision']}")
    lines.extend([
        f"event_log: {summary['event_log']}",
        f"stderr_log: {summary['stderr_log']}",
        f"final_message: {summary.get('final_message') or 'not available'}",
        f"diff_stat: {summary.get('diff_stat_file', 'not available')}",
        f"baseline: {summary.get('baseline_file', 'not available')}",
    ])
    if "raw_artifact_bytes" in summary:
        lines.append(
            f"output_compression: {summary.get('compression_percent', 0):.1f}% "
            f"({summary.get('summary_bytes', 0)}/{summary['raw_artifact_bytes']} bytes)"
        )
    text = "\n".join(lines) + "\n"
    if len(text) <= limit:
        return text
    suffix = "\n[summary truncated; full artifacts remain at run_dir]\n"
    return text[: max(0, limit - len(suffix))] + suffix
