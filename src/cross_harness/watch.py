from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO
import json
import os
import re
import shutil
import textwrap
import time
import unicodedata

from .config import load_config
from .paths import user_paths


_VERDICTS = {"success", "failed", "blocked", "partial"}
_DETAIL_LIMIT = 120
_DEFAULT_WIDTH = 100
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


@dataclass(frozen=True)
class EventLine:
    """A safe, structured watch line before terminal rendering."""

    marker: str
    label: str = ""
    detail: str = ""
    noise: bool = False
    tone: str = ""
    wrap: bool = False


_ANSI_ESCAPE = re.compile(
    r"\x1B(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x1B\x07]*(?:\x07|\x1B\\)|[\x00-\x7F])"
)


def _safe_text(value: object, *, collapse_whitespace: bool = False) -> str:
    """Remove terminal controls from an allow-listed string value."""
    text = _ANSI_ESCAPE.sub("", str(value or ""))
    text = "".join(char for char in text if char == "\n" or unicodedata.category(char) != "Cc")
    return " ".join(text.split()) if collapse_whitespace else text


def _command(value: object) -> str:
    return _safe_text(value, collapse_whitespace=True)


def _short_detail(value: object) -> str:
    detail = _command(value)
    if len(detail) <= _DETAIL_LIMIT:
        return detail
    return detail[: _DETAIL_LIMIT - 1] + "…"


def _command_output(value: object) -> tuple[EventLine, ...]:
    output = _safe_text(value)
    if not output.strip():
        return ()
    lines = output.splitlines()
    omitted = max(0, len(lines) - 10)
    rendered: list[EventLine] = []
    if omitted:
        rendered.append(EventLine("⎿", detail=f"… ({omitted} lines omitted)", tone="dim", wrap=True))
    rendered.extend(EventLine("⎿", detail=line, wrap=True) for line in lines[-10:])
    return tuple(rendered)


def _usage_line(usage: object) -> EventLine:
    data = usage if isinstance(usage, dict) else {}
    incoming = data.get("input_tokens", 0)
    outgoing = data.get("output_tokens", 0)
    incoming = incoming if isinstance(incoming, int) and not isinstance(incoming, bool) else 0
    outgoing = outgoing if isinstance(outgoing, int) and not isinstance(outgoing, bool) else 0
    return EventLine("⎿", detail=f"{_token_count(incoming)} in / {_token_count(outgoing)} out", tone="dim")


def _token_count(value: int) -> str:
    if abs(value) < 1000:
        return str(value)
    rendered = f"{value / 1000:.1f}".rstrip("0").rstrip(".")
    return f"{rendered}k"


def _error_text(event: dict) -> str:
    error = event.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    message = event.get("message")
    return message if isinstance(message, str) else "error"


def _claude_tool_target(name: str, inputs: dict) -> str:
    if name == "Bash":
        return _command(inputs.get("command"))
    if name in {"Read", "Edit", "Write"}:
        value = inputs.get("file_path")
    elif name in {"Grep", "Glob"}:
        value = inputs.get("pattern")
    else:
        return ""
    return _short_detail(value) if isinstance(value, str) else ""


def _codex_change_label(kind: object) -> str:
    return {
        "modified": "Edit",
        "update": "Edit",
        "added": "Write",
        "created": "Write",
        "create": "Write",
        "deleted": "Delete",
        "delete": "Delete",
    }.get(kind, "Edit")


def _noise(detail: str) -> tuple[EventLine, ...]:
    # Details here are fixed event names, never event payloads.
    return (EventLine("·", detail=detail, noise=True),)


def describe_event(event: dict) -> tuple[EventLine, ...]:
    """Describe only allow-listed fields from a Codex or Claude stream event."""
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return _noise("event")

    if event_type == "assistant":
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return _noise("assistant")
        lines: list[EventLine] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                lines.append(EventLine("›", detail=_safe_text(block["text"]), wrap=True))
            elif block_type == "tool_use" and isinstance(block.get("name"), str):
                inputs = block.get("input") if isinstance(block.get("input"), dict) else {}
                lines.append(EventLine(
                    "⏺",
                    label=block["name"],
                    detail=_claude_tool_target(block["name"], inputs),
                    wrap=block["name"] == "Bash",
                ))
            elif block_type == "thinking":
                lines.append(EventLine("·", detail="thinking", noise=True))
        return tuple(lines) or _noise("assistant")

    if event_type == "user":
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return _noise("user")
        results = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]
        if not results:
            return _noise("user")
        failed = sum(block.get("is_error") is True for block in results)
        status = "error" if failed else "ok"
        return (EventLine("⎿", detail=f"{status} ({len(results)})", tone="red" if failed else "dim"),)

    if event_type == "rate_limit_event":
        info = event.get("rate_limit_info")
        status = info.get("status") if isinstance(info, dict) else None
        if isinstance(status, str) and status != "allowed":
            return (EventLine("⚠", label="rate limit", detail=status, tone="yellow"),)
        return _noise("rate_limit_event")

    if event_type == "result":
        return (_usage_line(event.get("usage")),)

    if event_type in {"turn.failed", "error"}:
        return (EventLine("✖", label="error", detail=_error_text(event), tone="red"),)

    if event_type == "turn.completed":
        return (_usage_line(event.get("usage")),)

    if event_type.startswith("item."):
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_type = item.get("type")
        if event_type == "item.started" and item_type == "command_execution":
            return (EventLine("⏺", label="Bash", detail=_command(item.get("command")), wrap=True),)
        if event_type != "item.completed":
            return _noise("item.started" if event_type == "item.started" else "item")
        if item_type == "command_execution":
            lines = list(_command_output(item.get("aggregated_output")))
            if isinstance(item.get("exit_code"), int) and not isinstance(item.get("exit_code"), bool):
                code = item["exit_code"]
                lines.append(EventLine("⎿", detail=f"exit {code}", tone="dim" if code == 0 else "red"))
            return tuple(lines) or _noise("command_execution")
        if item_type == "file_change":
            changes = item.get("changes")
            if not isinstance(changes, list):
                return _noise("file_change")
            return tuple(
                EventLine("⏺", label=_codex_change_label(change.get("kind")), detail=_short_detail(change["path"]))
                for change in changes
                if isinstance(change, dict) and isinstance(change.get("path"), str)
            ) or _noise("file_change")
        if item_type == "agent_message" and isinstance(item.get("text"), str):
            return (EventLine("›", detail=_safe_text(item["text"]), wrap=True),)
        if item_type == "reasoning" and isinstance(item.get("text"), str):
            return (EventLine("✻", detail=_safe_text(item["text"]), wrap=True),)
        if item_type == "web_search" and isinstance(item.get("query"), str):
            return (EventLine("⏺", label="Search", detail=_short_detail(item["query"])),)
        return _noise("item.completed")

    if event_type in {"thread.started", "turn.started", "system"}:
        return _noise(event_type)
    return _noise("event")


def _styled(value: str, code: str, color: bool) -> str:
    return f"{code}{value}{_RESET}" if color and code else value


def _render_one(line: EventLine, *, width: int, color: bool) -> list[str]:
    marker = _styled(line.marker, _CYAN if line.marker in {"⏺", "›", "✻"} else "", color)
    if line.label:
        label = _styled(f"{line.label:<7}", _BOLD + _CYAN, color)
        prefix = f"  {marker} {label}"
    else:
        prefix = f"  {marker} "
    if not line.wrap:
        detail = _styled(line.detail, {"dim": _DIM, "red": _RED, "green": _GREEN, "yellow": _YELLOW}.get(line.tone, ""), color)
        return [f"{prefix}{detail}".rstrip()]

    # ANSI escapes are deliberately absent from wrapped message body width math.
    raw_prefix = f"  {line.marker} " + (f"{line.label:<7}" if line.label else "")
    available = max(1, width - len(raw_prefix))
    body = line.detail
    if line.marker == "›":
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and "status" in payload:
                status = _safe_text(payload.get("status"))
                work_completed = _safe_text(payload.get("work_completed"))
                changed_files = payload.get("changed_files")
                tests = payload.get("tests")
                rendered_status = status if status in _VERDICTS else "unknown"
                rendered_files = [_safe_text(value) for value in changed_files] if isinstance(changed_files, list) else []
                rendered_tests = [_safe_text(value) for value in tests] if isinstance(tests, list) else []
                rows = [f"status: {rendered_status}"]
                if work_completed:
                    rows.extend(["work_completed:", work_completed])
                if rendered_files:
                    rows.extend(["changed_files:", *[f"- {value}" for value in rendered_files]])
                if rendered_tests:
                    rows.extend(["tests:", *[f"- {value}" for value in rendered_tests]])
                if payload.get("error") is not None:
                    rows.append(f"error: {_safe_text(payload.get('error'))}")
                if payload.get("next_decision") is not None:
                    rows.append(f"next_decision: {_safe_text(payload.get('next_decision'))}")
                body = "\n".join(rows)
        except (TypeError, json.JSONDecodeError):
            pass

    def display_width(value: str) -> int:
        return sum(
            0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
            for char in value
        )

    def split_paragraph(paragraph: str) -> list[str]:
        tokens = textwrap.TextWrapper(replace_whitespace=False, drop_whitespace=False).wordsep_re.split(paragraph)
        chunks: list[str] = []
        chunk = ""
        for token in tokens:
            if not token:
                continue
            if chunk and display_width(chunk) + display_width(token) > available:
                chunks.append(chunk)
                chunk = ""
            while display_width(token) > available:
                fitting = ""
                for char in token:
                    if fitting and display_width(fitting) + display_width(char) > available:
                        break
                    fitting += char
                if chunk:
                    chunks.append(chunk)
                    chunk = ""
                chunks.append(fitting)
                token = token[len(fitting):]
            chunk += token
        if chunk or not chunks:
            chunks.append(chunk)
        return chunks

    chunks = [chunk for paragraph in body.split("\n") for chunk in split_paragraph(paragraph)]
    detail_tone = {"dim": _DIM, "red": _RED, "green": _GREEN, "yellow": _YELLOW}.get(line.tone, "")
    first = f"{prefix}{_styled(chunks[0], detail_tone, color)}"
    continuation = " " * len(raw_prefix)
    return [first, *(f"{continuation}{_styled(chunk, detail_tone, color)}" for chunk in chunks[1:])]


def render_lines(lines: Iterable[EventLine], *, width: int = _DEFAULT_WIDTH, color: bool = False, show_all: bool = False) -> list[str]:
    """Render structured event descriptions without run-directory prefixes."""
    rendered: list[str] = []
    for line in lines:
        if line.noise and not show_all:
            continue
        rows = _render_one(line, width=max(1, width), color=color)
        if line.noise and color:
            rows = [_styled(row, _DIM, True) for row in rows]
        rendered.extend(rows)
    return rendered


def format_event(run_dir: Path, event: dict) -> str:
    """Compatibility helper for callers wanting a rendered event string."""
    del run_dir
    return "\n".join(render_lines(describe_event(event)))


def run_header(run_dir: Path, *, color: bool = False) -> str:
    """Return a defensive, human-readable header for an active run."""
    role = run_dir.name
    harness = ""
    try:
        record = json.loads((run_dir / "execution.json").read_text(encoding="utf-8"))
        if isinstance(record, dict):
            if isinstance(record.get("role_name"), str):
                role = record["role_name"]
            if isinstance(record.get("harness"), str):
                harness = record["harness"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    identity = f"{role} · {harness}" if harness else role
    value = f"── {time.strftime('%H:%M:%S')} · {identity} ──────"
    return _styled(value, _BOLD, color)


def newest_run(runs_root: Path) -> Path | None:
    """Return the newest run directory without creating or changing anything."""
    try:
        candidates = [path for path in runs_root.iterdir() if path.is_dir()]
        return max(candidates, key=lambda path: path.name, default=None)
    except OSError:
        return None


class RunWatcher:
    """Incrementally follow the newest run directory using read-only operations."""

    def __init__(self, runs_root: Path, *, show_all: bool = False, color: bool = False, width: int = _DEFAULT_WIDTH):
        self.runs_root = runs_root
        self.show_all = show_all
        self.color = color
        self.width = width
        self.run_dir: Path | None = None
        self._event_offset = 0
        self._verdict_rendered = False
        self._header_pending = False
        self._initial_scan = True

    def poll(self) -> list[str]:
        newest = newest_run(self.runs_root)
        initial_scan = self._initial_scan
        self._initial_scan = False
        if newest != self.run_dir:
            self._attach(newest, skip_existing=initial_scan)
        if self.run_dir is None:
            return []
        lines: list[str] = []
        if self._header_pending:
            self._header_pending = False
            lines.append(run_header(self.run_dir, color=self.color))
        return [*lines, *self._event_lines(), *self._verdict_line()]

    def _attach(self, run_dir: Path | None, skip_existing: bool) -> None:
        self.run_dir = run_dir
        self._event_offset = 0
        self._verdict_rendered = False
        self._header_pending = False
        if run_dir is None:
            return
        status = self._state_status(run_dir)
        # A completed run present when watch starts is historical and must remain
        # entirely quiet.  A run that finishes after watch has started still gets
        # its one final verdict.
        self._verdict_rendered = skip_existing and status in _VERDICTS
        self._header_pending = status not in _VERDICTS
        if not skip_existing:
            return
        try:
            self._event_offset = (run_dir / "events.jsonl").stat().st_size
        except OSError:
            pass

    @staticmethod
    def _state_status(run_dir: Path) -> object:
        try:
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return state.get("status") if isinstance(state, dict) else None

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
                lines.extend(render_lines(describe_event(event), width=self.width, color=self.color, show_all=self.show_all))
        return lines

    def _verdict_line(self) -> list[str]:
        assert self.run_dir is not None
        if self._verdict_rendered:
            return []
        status = self._state_status(self.run_dir)
        if status not in _VERDICTS:
            return []
        self._verdict_rendered = True
        marker, tone = {
            "success": ("✔", "green"),
            "failed": ("✖", "red"),
            "blocked": ("⚠", "yellow"),
            "partial": ("◐", "yellow"),
        }[status]
        return render_lines((EventLine(marker, detail=status, tone=tone),), width=self.width, color=self.color)


def watch(
    config_path: Path | None = None,
    home: Path | None = None,
    output: TextIO | None = None,
    poll_seconds: float = 0.25,
    *,
    show_all: bool = False,
    color: str = "auto",
) -> int:
    """Follow delegation activity until interrupted, without mutating runtime state."""
    if poll_seconds <= 0:
        raise ValueError("poll seconds must be positive")
    if color not in {"auto", "always", "never"}:
        raise ValueError("color must be auto, always, or never")
    paths = user_paths(home)
    config = load_config(config_path, paths.home)
    stream = output
    if stream is None:
        import sys
        stream = sys.stdout
    is_tty = getattr(stream, "isatty", lambda: False)()
    use_color = color == "always" or (color == "auto" and "NO_COLOR" not in os.environ and is_tty)
    width = shutil.get_terminal_size(fallback=(_DEFAULT_WIDTH, 24)).columns if is_tty else _DEFAULT_WIDTH
    watcher = RunWatcher(Path(config["runtime_root"]) / "runs", show_all=show_all, color=use_color, width=width)
    try:
        while True:
            for line in watcher.poll():
                print(line, file=stream, flush=True)
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        return 0
