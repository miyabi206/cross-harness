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


def parse_events(path: Path) -> dict:
    result = {"thread_id": None, "usage": {}, "errors": [], "commands": [], "blocked_category": None}
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
            if isinstance(item, dict) and item.get("type") == "command_execution":
                if item.get("status") not in {None, "completed", "success"} or item.get("exit_code") not in {None, 0}:
                    result["commands"].append({
                        "command": item.get("command", ""),
                        "exit_code": item.get("exit_code"),
                        "output": str(item.get("aggregated_output", item.get("output", "")))[-4000:],
                    })
    result["errors"] = [text for text in result["errors"] if text]
    return result


def _parse_claude_tool_events(event: dict, commands: dict[str, str], result: dict) -> None:
    """Record failed Claude Bash tool results as command failures."""
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
        if not isinstance(block, dict) or block.get("type") != "tool_result" or block.get("is_error") is not True:
            continue
        text = _tool_result_text(block)
        tool_id = block.get("tool_use_id")
        command = commands.get(tool_id) if isinstance(tool_id, str) else None
        if command is None:
            continue
        result["commands"].append({"command": command, "exit_code": 1, "output": text[-4000:]})


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
    """Extract terminal Claude failures from stream-json's structured fields.

    Claude emits rate limits as a dedicated event and reports error categories
    on ``result`` and ``system/api_retry`` events. Deliberately do not inspect
    free-form messages here: the runner's legacy text patterns are retained for
    Codex event streams only.
    """
    event_type = event.get("type")
    if event_type == "rate_limit_event":
        info = event.get("rate_limit_info")
        return "rate_limit" if isinstance(info, dict) and info.get("status") == "rejected" else None
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


def render_summary(summary: dict, limit: int) -> str:
    lines = [
        f"status: {summary['status']}",
        f"run_dir: {summary['run_dir']}",
        f"exit_code: {summary['exit_code']}",
        f"role: {summary['role']}",
        f"model: {summary['model']}",
        f"effort: {summary['effort']}",
        f"changed_files: {', '.join(summary.get('changed_files', [])) or 'none'}",
        f"tests: {'; '.join(summary.get('tests', [])) or 'not reported'}",
    ]
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
        f"final_message: {summary['final_message']}",
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
