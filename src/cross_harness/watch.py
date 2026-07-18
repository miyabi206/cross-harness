from __future__ import annotations

from pathlib import Path
from typing import TextIO
import json
import time

from .config import load_config
from .paths import user_paths


_VERDICTS = {"success", "failed", "blocked", "partial"}
_COMMAND_LIMIT = 120


def _short_command(value: object) -> str:
    command = " ".join(str(value or "").split())
    if len(command) <= _COMMAND_LIMIT:
        return command
    return command[: _COMMAND_LIMIT - 1] + "…"


def format_event(run_dir: Path, event: dict) -> str:
    """Render a safe, single-line description of one executor event."""
    event_type = event.get("type")
    prefix = f"[{run_dir.name}]"
    if not isinstance(event_type, str):
        return f"{prefix} event"
    if event_type == "assistant":
        details = _claude_tool_use_details(event)
        suffix = f": {', '.join(details)}" if details else ""
        return f"{prefix} assistant tool_use{suffix}"
    if event_type == "user":
        details = _claude_tool_result_details(event)
        suffix = f": {', '.join(details)}" if details else ""
        return f"{prefix} user tool_result{suffix}"
    if not event_type.startswith("item."):
        return f"{prefix} {event_type}"

    item = event.get("item")
    item = item if isinstance(item, dict) else {}
    item_type = item.get("type")
    item_type = item_type if isinstance(item_type, str) else "unknown"
    details: list[str] = []
    if item_type == "command_execution":
        command = _short_command(item.get("command"))
        if command:
            details.append(command)
        if "exit_code" in item and isinstance(item["exit_code"], int):
            details.append(f"exit_code={item['exit_code']}")
    elif item_type == "file_change":
        changes = item.get("changes")
        if isinstance(changes, list):
            for change in changes:
                if not isinstance(change, dict) or not isinstance(change.get("path"), str):
                    continue
                kind = change.get("kind")
                details.append(f"{kind} {change['path']}" if isinstance(kind, str) else change["path"])
    suffix = f": {', '.join(details)}" if details else ""
    return f"{prefix} {event_type} {item_type}{suffix}"


def _claude_tool_use_details(event: dict) -> list[str]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    details: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        inputs = block.get("input")
        if not isinstance(name, str) or not isinstance(inputs, dict):
            continue
        if name == "Bash":
            command = _short_command(inputs.get("command"))
            if command:
                details.append(f"Bash {command}")
        elif name in {"Edit", "Write"}:
            file_path = inputs.get("file_path")
            if isinstance(file_path, str) and file_path:
                details.append(f"{name} {file_path}")
    return details


def _claude_tool_result_details(event: dict) -> list[str]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    count = sum(
        1
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    if count == 0:
        return []
    failures = sum(
        1
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and block.get("is_error") is True
    )
    return [f"failed={failures}" if failures else f"completed={count}"]


def newest_run(runs_root: Path) -> Path | None:
    """Return the newest run directory without creating or changing anything."""
    try:
        candidates = [path for path in runs_root.iterdir() if path.is_dir()]
        return max(candidates, key=lambda path: path.name, default=None)
    except OSError:
        return None


class RunWatcher:
    """Incrementally follow the newest run directory using read-only operations."""

    def __init__(self, runs_root: Path):
        self.runs_root = runs_root
        self.run_dir: Path | None = None
        self._event_offset = 0
        self._verdict_rendered = False
        self._initial_scan = True

    def poll(self) -> list[str]:
        newest = newest_run(self.runs_root)
        initial_scan = self._initial_scan
        self._initial_scan = False
        if newest != self.run_dir:
            self._attach(newest, skip_existing=initial_scan)
        if self.run_dir is None:
            return []
        return [*self._event_lines(), *self._verdict_line()]

    def _attach(self, run_dir: Path | None, skip_existing: bool) -> None:
        self.run_dir = run_dir
        self._event_offset = 0
        self._verdict_rendered = False
        if run_dir is None or not skip_existing:
            return
        try:
            self._event_offset = (run_dir / "events.jsonl").stat().st_size
        except OSError:
            pass
        try:
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self._verdict_rendered = isinstance(state, dict) and state.get("status") in _VERDICTS

    def _event_lines(self) -> list[str]:
        assert self.run_dir is not None
        path = self.run_dir / "events.jsonl"
        try:
            with path.open("rb") as handle:
                handle.seek(self._event_offset)
                data = handle.read()
        except OSError:
            return []
        newline = data.rfind(b"\n")
        if newline < 0:
            return []
        complete = data[: newline + 1]
        self._event_offset += len(complete)
        lines: list[str] = []
        for raw in complete.splitlines():
            try:
                event = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                lines.append(format_event(self.run_dir, event))
        return lines

    def _verdict_line(self) -> list[str]:
        assert self.run_dir is not None
        if self._verdict_rendered:
            return []
        try:
            state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        status = state.get("status") if isinstance(state, dict) else None
        if status not in _VERDICTS:
            return []
        self._verdict_rendered = True
        return [f"[{self.run_dir.name}] verdict: {status}"]


def watch(config_path: Path | None = None, home: Path | None = None, output: TextIO | None = None, poll_seconds: float = 0.25) -> int:
    """Follow delegation activity until interrupted, without mutating runtime state."""
    if poll_seconds <= 0:
        raise ValueError("poll seconds must be positive")
    paths = user_paths(home)
    config = load_config(config_path, paths.home)
    watcher = RunWatcher(Path(config["runtime_root"]) / "runs")
    stream = output
    if stream is None:
        import sys
        stream = sys.stdout
    try:
        while True:
            for line in watcher.poll():
                print(line, file=stream, flush=True)
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        return 0
