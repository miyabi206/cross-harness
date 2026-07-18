from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys

from .auth import detected_api_keys, sanitized_environment, verify_codex_chatgpt
from .config import load_config
from .files import atomic_write, dump_json
from .maintenance import cleanup
from .paths import user_paths


SHELL_BOUNDARY = r"[;&|()\s'\"`]"
CODEX_EXEC = re.compile(rf"(?:^|{SHELL_BOUNDARY})(?:[^\s;&|()'\"`]*/)?codex\s+(?:e|exec)(?=$|{SHELL_BOUNDARY})")
CLAUDE_EXEC = re.compile(rf"(?:^|{SHELL_BOUNDARY})(?:[^\s;&|()'\"`]*/)?claude(?=$|{SHELL_BOUNDARY})")
BARE_WRAPPER = re.compile(rf"(?:^|{SHELL_BOUNDARY})cross-harness\s+(?:task|delegate|retry)(?=$|{SHELL_BOUNDARY})")
HARNESS_DELEGATION = re.compile(
    rf"(?:^|{SHELL_BOUNDARY})(?:[^\s;&|()'\"`]*/)?cross-harness\s+(?:delegate|retry)(?=$|{SHELL_BOUNDARY})"
)


def _input() -> dict:
    try:
        value = json.load(sys.stdin)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _tool(data: dict) -> tuple[str, str]:
    name = str(data.get("tool_name", data.get("toolName", "")))
    details = data.get("tool_input", data.get("toolInput", {}))
    command = str(details.get("command", "")) if isinstance(details, dict) else ""
    return name, command


def _deny(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def claude_pre_tool_use() -> int:
    name, command = _tool(_input())
    if name in {"Edit", "Write"}:
        return _deny("cross-harness: Claude is the orchestrator; delegate project edits through cross-harness")
    if name == "Bash" and CODEX_EXEC.search(command):
        return _deny("cross-harness: direct codex exec is blocked; use cross-harness delegate with a task file")
    if name == "Bash" and BARE_WRAPPER.search(command):
        expected = user_paths().executable.resolve()
        resolved = shutil.which("cross-harness")
        if not resolved or Path(resolved).resolve() != expected:
            return _deny("cross-harness: bare wrapper command does not resolve to the installed personal executable")
    return 0


def codex_pre_tool_use() -> int:
    _, command = _tool(_input())
    if CLAUDE_EXEC.search(command):
        return _deny("cross-harness: nested Claude launch from Codex is blocked")
    if CODEX_EXEC.search(command) or HARNESS_DELEGATION.search(command):
        return _deny("cross-harness: nested executor launch from delegated Codex is blocked")
    return 0


def claude_session_start(home: Path | None = None) -> int:
    paths = user_paths(home)
    if os.environ.get("CROSS_HARNESS_ACTIVE") == "1":
        return _deny("cross-harness: nested Claude launch from a delegated Codex run is blocked")
    warnings: list[str] = []
    keys = detected_api_keys()
    if keys:
        warnings.append("API-key environment detected; Codex delegation is disabled until removed: " + ", ".join(keys))
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
            env=sanitized_environment(paths.home),
            check=False,
        )
        compact = f"{result.stdout}\n{result.stderr}".replace(" ", "").lower()
        if result.returncode or '"loggedin":true' not in compact:
            warnings.append("Claude Code is not authenticated; automatic orchestration cannot be verified")
    except (OSError, subprocess.TimeoutExpired):
        warnings.append("Claude authentication status is unavailable")
    if not keys:
        try:
            config = load_config(home=paths.home)
            verify_codex_chatgpt(
                Path(config["runtime_root"]),
                paths.home,
                config["auth_cache_hours"],
            )
        except Exception as exc:
            warnings.append(f"Codex ChatGPT authentication is unavailable: {exc}")
    try:
        cleanup(home=paths.home)
    except Exception as exc:  # hooks must not hide the session for maintenance failure
        warnings.append(f"runtime cleanup warning: {exc}")
    state_file = paths.home / ".local/state/cross-harness/session/latest.json"
    if state_file.exists():
        try:
            state = state_file.read_text(encoding="utf-8")[:4000]
            warnings.append("Previous compact session state:\n" + state)
        except OSError:
            pass
    if warnings:
        print("<cross-harness-session>\n" + "\n".join(warnings) + "\n</cross-harness-session>")
    else:
        print("<cross-harness-session>Subscription checks passed; delegation wrapper is available.</cross-harness-session>")
    return 0


def claude_stop(home: Path | None = None) -> int:
    paths = user_paths(home)
    data = _input()
    config = load_config(home=paths.home)
    runtime = Path(config["runtime_root"])
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "cwd": data.get("cwd", os.getcwd()),
        "session_id": data.get("session_id", data.get("sessionId")),
        "stop_hook_active": data.get("stop_hook_active", data.get("stopHookActive", False)),
        "note": "Inspect the latest run summary before resuming unfinished delegated work.",
    }
    atomic_write(runtime / "session/latest.json", dump_json(payload))
    return 0


def run_hook(name: str, home: Path | None = None) -> int:
    handlers = {
        "claude-pre-tool-use": claude_pre_tool_use,
        "codex-pre-tool-use": codex_pre_tool_use,
        "claude-session-start": lambda: claude_session_start(home),
        "claude-stop": lambda: claude_stop(home),
    }
    if name not in handlers:
        print(f"unknown hook: {name}", file=sys.stderr)
        return 2
    return handlers[name]()
