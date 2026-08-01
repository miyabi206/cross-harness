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
from .config import effective_mode, load_config
from .errors import ConfigError
from .files import atomic_write, dump_json
from .maintenance import cleanup
from .paths import user_paths
from .installer import synchronize_claude_agent_roles
from .taskfile import contains_secret


SHELL_BOUNDARY = r"[;&|()\s'\"`]"
CODEX_EXEC = re.compile(rf"(?:^|{SHELL_BOUNDARY})(?:[^\s;&|()'\"`]*/)?codex\s+(?:e|exec)(?=$|{SHELL_BOUNDARY})")
CLAUDE_EXEC = re.compile(rf"(?:^|{SHELL_BOUNDARY})(?:[^\s;&|()'\"`]*/)?claude(?=$|{SHELL_BOUNDARY})")
BARE_WRAPPER = re.compile(rf"(?:^|{SHELL_BOUNDARY})cross-harness\s+(?:task|delegate|retry)(?=$|{SHELL_BOUNDARY})")
HARNESS_DELEGATION = re.compile(
    rf"(?:^|{SHELL_BOUNDARY})(?:[^\s;&|()'\"`]*/)?cross-harness\s+(?:delegate|retry)(?=$|{SHELL_BOUNDARY})"
)
HARNESS_REDELEGATION = re.compile(
    rf"(?:^|{SHELL_BOUNDARY})(?:[^\s;&|()'\"`]*/)?cross-harness\s+(?:delegate|retry|task\s+create)(?=$|{SHELL_BOUNDARY})"
)
SIMPLE_WRAPPER_UNSAFE_SYNTAX = re.compile(r"[;&|()\n\r`<>{}]")
ORCHESTRATOR_ACTIONS_MAX_BYTES = 5 * 1024 * 1024
ORCHESTRATOR_ACTIONS_ARCHIVES = 4


def _installed_wrapper_arguments(command: str) -> str | None:
    """Return task arguments only for an unambiguously single wrapper command.

    This intentionally does not parse shell syntax.  It recognizes just the
    installed wrapper's literal absolute path at the start of a command and
    rejects every shell construct that could introduce another command.  Only
    ``task`` is exempt because its text arguments are not executed; other
    wrapper subcommands may launch an executor themselves.
    """
    executable = user_paths().executable
    if not executable.is_absolute():
        return None
    expected = str(executable)
    if not command.startswith(expected):
        return None
    arguments = command[len(expected):]
    if arguments and arguments[0] not in " \t":
        return None
    if SIMPLE_WRAPPER_UNSAFE_SYNTAX.search(command):
        return None
    words = arguments.lstrip(" \t").split(maxsplit=1)
    if not words or words[0] != "task":
        return None
    return arguments


def _input() -> dict | None:
    try:
        value = json.load(sys.stdin)
        return value if isinstance(value, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _tool(data: dict | None) -> tuple[str, str] | None:
    """Extract a tool invocation, rejecting incomplete hook input.

    A command is required only for Bash.  Other tools are safe to classify by
    name alone, including when their input is absent or uses an unexpected
    shape.
    """
    if data is None:
        return None
    name = data.get("tool_name", data.get("toolName"))
    if not isinstance(name, str):
        return None
    if name != "Bash":
        return name, ""
    details = data.get("tool_input", data.get("toolInput"))
    if not isinstance(details, dict):
        return None
    command = details.get("command")
    if not isinstance(command, str):
        return None
    return name, command


def _file_path(data: dict | None) -> str | None:
    """Extract an Edit or Write target path, failing closed on malformed input."""
    if data is None:
        return None
    details = data.get("tool_input", data.get("toolInput"))
    if not isinstance(details, dict):
        return None
    path = details.get("file_path")
    return path if isinstance(path, str) and path else None


def _cwd(data: dict | None) -> Path | None:
    """Extract an existing absolute cwd from hook input, failing closed otherwise."""
    if data is None:
        return None
    value = data.get("cwd")
    if not isinstance(value, str) or not value:
        return None
    try:
        cwd = Path(value)
        if not cwd.is_absolute():
            return None
        return cwd.resolve(strict=True) if cwd.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _mode_is_off(config: dict, data: dict | None) -> bool:
    """Allow opt-out only when both config and cwd resolution are trustworthy."""
    cwd = _cwd(data)
    if cwd is None:
        return False
    try:
        return effective_mode(config, cwd) == "off"
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, ConfigError):
        return False


def _git_root_from_cwd(cwd: Path | None) -> Path | None:
    """Find the Git root by walking ancestors from an already-resolved cwd."""
    if cwd is None:
        return None
    try:
        current = cwd
        while True:
            marker = current / ".git"
            git_dir = marker
            if marker.is_file():
                prefix = "gitdir:"
                contents = marker.read_text(encoding="utf-8", errors="surrogateescape").strip()
                if contents.lower().startswith(prefix):
                    git_dir = Path(contents[len(prefix):].strip())
                    if not git_dir.is_absolute():
                        git_dir = current / git_dir
            if git_dir.is_dir() and (git_dir / "HEAD").is_file():
                return current
            parent = current.parent
            if parent == current:
                return None
            current = parent
    except (OSError, RuntimeError, ValueError):
        return None


def _orchestrator_write_path_is_allowed(file_path: str | None, cwd: Path | None) -> bool:
    """Allow Claude metadata and mechanical edits inside the cwd's Git worktree."""
    if file_path is None:
        return False
    try:
        claude_root = user_paths().claude.resolve()
        target = Path(file_path).resolve()
        plans = (claude_root / "plans").resolve()
        if plans.is_relative_to(claude_root) and target.is_relative_to(plans):
            return True
        projects = (claude_root / "projects").resolve()
        if not projects.is_relative_to(claude_root):
            return False
        if target.is_relative_to(projects):
            relative = target.relative_to(projects)
            if len(relative.parts) >= 2 and relative.parts[1] == "memory":
                return True

        git_root = _git_root_from_cwd(cwd)
        if git_root is None or not target.is_relative_to(git_root):
            return False
        return not target.is_relative_to(git_root / ".git")
    except (OSError, RuntimeError, ValueError, TypeError):
        return False


def _normalized_command(command: str) -> str:
    """Remove shell word-splitting quotes and backslash escapes for matching."""
    normalized: list[str] = []
    index = 0
    while index < len(command):
        character = command[index]
        if character == "\\" and index + 1 < len(command):
            normalized.append(command[index + 1])
            index += 2
        elif character in "'\"":
            index += 1
        else:
            normalized.append(character)
            index += 1
    return "".join(normalized)


def _matches(pattern: re.Pattern[str], command: str) -> bool:
    return bool(pattern.search(command) or pattern.search(_normalized_command(command)))


def _deny(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def _delegated_claude_execution() -> dict | None:
    """Return a runner-issued Claude execution record, or None on any uncertainty."""
    raw_run_dir = os.environ.get("CROSS_HARNESS_RUN_DIR")
    if not raw_run_dir:
        return None
    try:
        config = load_config(home=user_paths().home)
        runtime_root = Path(config["runtime_root"]).resolve(strict=True)
        run_dir = Path(raw_run_dir).resolve(strict=True)
        if not run_dir.is_dir():
            return None
        run_dir.relative_to(runtime_root)
        record_path = run_dir / "execution.json"
        if not record_path.is_file():
            return None
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError, ConfigError):
        return None
    if not isinstance(record, dict):
        return None
    if (
        not isinstance(record.get("role_name"), str)
        or record.get("harness") != "claude"
        or not isinstance(record.get("write"), bool)
        or not isinstance(record.get("kind"), str)
        or not isinstance(record.get("cwd"), str)
    ):
        return None
    return record


def _orchestrator_action_target(data: dict | None, name: str, command: str) -> str | None:
    if name == "Bash":
        return command
    return _file_path(data)


def _rotate_orchestrator_actions(path: Path) -> None:
    """Keep a bounded set of action logs, with the newest records in the base file."""
    oldest = path.with_name(f"{path.stem}.{ORCHESTRATOR_ACTIONS_ARCHIVES}{path.suffix}")
    oldest.unlink(missing_ok=True)
    for index in range(ORCHESTRATOR_ACTIONS_ARCHIVES - 1, 0, -1):
        source = path.with_name(f"{path.stem}.{index}{path.suffix}")
        if source.exists():
            source.replace(path.with_name(f"{path.stem}.{index + 1}{path.suffix}"))
    path.replace(path.with_name(f"{path.stem}.1{path.suffix}"))


def _record_orchestrator_action(data: dict | None, name: str, command: str, allowed: bool) -> None:
    """Best-effort, bounded audit logging that cannot affect a hook decision."""
    if name not in {"Edit", "Write", "Bash"}:
        return
    try:
        target = _orchestrator_action_target(data, name, command)
        cwd = data.get("cwd") if isinstance(data, dict) and isinstance(data.get("cwd"), str) else None
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_name": name,
            "decision": "allowed" if allowed else "denied",
        }
        if any(contains_secret(value) for value in (target, cwd) if value is not None):
            record["redacted"] = True
        else:
            record["target"] = target
            record["cwd"] = cwd

        config = load_config(home=user_paths().home)
        runtime_root = Path(config["runtime_root"])
        runtime_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(runtime_root, 0o700)
        path = runtime_root / "orchestrator-actions.jsonl"
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        if path.exists() and path.stat().st_size + len(line.encode("utf-8")) > ORCHESTRATOR_ACTIONS_MAX_BYTES:
            _rotate_orchestrator_actions(path)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        return


def claude_pre_tool_use() -> int:
    data = _input()
    tool = _tool(data)
    if tool is None:
        return _deny("cross-harness: invalid tool hook input is blocked")
    name, command = tool
    execution = _delegated_claude_execution()
    if execution is not None:
        if name in {"Edit", "Write"} and not execution["write"]:
            return _deny("cross-harness: delegated Claude has read-only access")
        if name == "Bash" and (
            _matches(CODEX_EXEC, command)
            or _matches(CLAUDE_EXEC, command)
            or _matches(HARNESS_REDELEGATION, command)
        ):
            return _deny("cross-harness: nested executor launch from delegated Claude is blocked")
        return 0
    if name in {"Edit", "Write"}:
        result: int | None = None
        try:
            if _mode_is_off(load_config(home=user_paths().home), data):
                result = 0
        except (OSError, RuntimeError, ValueError, TypeError, ConfigError):
            pass
        if result is None:
            if _orchestrator_write_path_is_allowed(_file_path(data), _cwd(data)):
                result = 0
            else:
                result = _deny("cross-harness: Claude is the orchestrator; delegate project edits through cross-harness")
        _record_orchestrator_action(data, name, command, result == 0)
        return result
    wrapper_arguments = _installed_wrapper_arguments(command)
    executor_scan = command if wrapper_arguments is None else ""
    if name == "Bash" and _matches(CODEX_EXEC, executor_scan):
        result = _deny("cross-harness: direct codex exec is blocked; use cross-harness delegate with a task file")
    elif name == "Bash" and _matches(BARE_WRAPPER, command):
        expected = user_paths().executable.resolve()
        resolved = shutil.which("cross-harness")
        if not resolved or Path(resolved).resolve() != expected:
            result = _deny("cross-harness: bare wrapper command does not resolve to the installed personal executable")
        else:
            result = 0
    else:
        result = 0
    _record_orchestrator_action(data, name, command, result == 0)
    return result


def codex_pre_tool_use() -> int:
    tool = _tool(_input())
    if tool is None:
        return _deny("cross-harness: invalid tool hook input is blocked")
    _, command = tool
    wrapper_arguments = _installed_wrapper_arguments(command)
    executor_scan = command if wrapper_arguments is None else ""
    if _matches(CLAUDE_EXEC, executor_scan):
        return _deny("cross-harness: nested Claude launch from Codex is blocked")
    if _matches(CODEX_EXEC, executor_scan) or _matches(HARNESS_DELEGATION, command):
        return _deny("cross-harness: nested executor launch from delegated Codex is blocked")
    return 0


def claude_session_start(home: Path | None = None) -> int:
    paths = user_paths(home)
    executor = os.environ.get("CROSS_HARNESS_EXECUTOR")
    if executor == "codex" and os.environ.get("CROSS_HARNESS_ACTIVE") == "1":
        return _deny("cross-harness: nested Claude launch from a delegated Codex run is blocked")
    if executor == "claude":
        return 0
    if os.environ.get("CROSS_HARNESS_ACTIVE") == "1":
        return _deny("cross-harness: nested Claude launch from a delegated Codex run is blocked")
    data = _input()
    warnings: list[str] = []
    config = None
    try:
        config = load_config(home=paths.home)
        if _mode_is_off(config, data):
            warnings.append(
                "cross-harness is disabled for this cwd; ignore the managed orchestrator instructions in CLAUDE.md."
            )
        if (paths.claude / "agents").exists():
            warnings.extend(synchronize_claude_agent_roles(paths, config))
    except Exception as exc:  # hooks must not hide the session for synchronization failure
        warnings.append(f"Claude agent configuration sync warning: {exc}")
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
            if config is None:
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
    data = _input() or {}
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
