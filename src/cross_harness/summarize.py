from __future__ import annotations

from pathlib import Path
import hashlib
import json
import re


FAILURE_WORDS = re.compile(r"\b(error|failed|failure|exception|panic|traceback)\b", re.IGNORECASE)
VOLATILE = re.compile(r"\b(?:0x[0-9a-f]+|\d+(?:\.\d+)?(?:ms|s)?|[0-9a-f]{8,})\b", re.IGNORECASE)


def parse_events(path: Path) -> dict:
    result = {"thread_id": None, "usage": {}, "errors": [], "commands": []}
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
            kind = event.get("type")
            if kind == "thread.started":
                result["thread_id"] = event.get("thread_id")
            elif kind == "turn.completed":
                result["usage"] = event.get("usage", {})
            elif kind in {"turn.failed", "error"}:
                result["errors"].append(_event_text(event))
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


def _event_text(event: dict) -> str:
    for key in ("message", "error", "detail"):
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
