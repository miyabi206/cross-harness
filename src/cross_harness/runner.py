from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from .auth import (
    sanitized_environment,
    verify_claude_config_ownership,
    verify_claude_subscription,
    verify_codex_chatgpt,
    verify_codex_config_ownership,
)
from .config import CLAUDE_EFFORTS, CODEX_EFFORTS, delegation_kind_error, load_config, project_config
from .errors import AuthError, ConfigError, DirtyWorktreeError, HarnessError, SupervisorDiedError
from .files import atomic_write, dump_json, sha256
from .paths import source_root, user_paths
from .summarize import command_matches_check, failure_signature, load_final, parse_events, render_summary, summary_item_text
from .taskfile import contains_secret


RATE_LIMIT = re.compile(r"rate.?limit|usage.?limit|quota|too many requests", re.IGNORECASE)
AUTH_FAILURE = re.compile(r"unauthorized|authentication|not logged in|login required", re.IGNORECASE)
_CODEX_BENIGN_CACHE_STDERR = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ERROR "
    r"codex_models_manager::(?:cache: failed to load models cache|manager: failed to renew cache TTL):.*"
)
_CHECKS_HEADING = re.compile(r"^#{1,6}\s+Checks\s*$", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s+")
_CHECK_ITEM = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")
_GIT_GLOBAL_OPTION = (
    r"(?:-(?:C|c)[ \t]+\S+|--(?:git-dir|work-tree|namespace|config-env)"
    r"(?:[ \t]+\S+|=\S+)|--(?:no-pager|paginate|literal-pathspecs|glob-pathspecs|noglob-pathspecs|icase-pathspecs))"
)
_GIT_COMMAND_PREFIX = rf"(?:^|[;\n&|][ \t]*)git(?:[ \t]+{_GIT_GLOBAL_OPTION})*[ \t]+"
_GIT_SHOW_HEAD_REDIRECT = re.compile(
    _GIT_COMMAND_PREFIX + r"show[ \t]+HEAD(?:[~^][^\s:]*)?(?::[^\s]+)?[^\n>]*>[ \t]*([^\s;&|]+)"
)
_GIT_CHECKOUT_PATHSPEC = re.compile(
    _GIT_COMMAND_PREFIX + r"checkout(?:[ \t]+[^;&|\s]+)?[ \t]+--[ \t]+([^;&|\n]+)"
)
_GIT_RESTORE = re.compile(_GIT_COMMAND_PREFIX + r"restore\b([^;&|\n]*)")
_GIT_STASH = re.compile(_GIT_COMMAND_PREFIX + r"stash\b([^;&|\n]*)")
CLAUDE_INSPECTION_TOOLS = "Bash,Read,Grep,Glob"
CLAUDE_EXECUTOR_CHARTER = """# Cross-harness executor

You are the bounded execution worker for a task file supplied by Claude.
Do not follow the orchestrator charter from CLAUDE.md: this is an execution-role
charter. Make the smallest change that satisfies its completion conditions.
Do not ask the user questions, broaden scope, delegate to another agent, or launch Codex.
If a blocking unknown prevents safe work, return `blocked` with the single decision needed.

Your final response must contain exactly these six fields through the supplied
JSON schema: status, work_completed, changed_files, tests, error, and
next_decision. On failure, include exit code, cause, file, line, expected value,
and actual value whenever those facts exist. Do not narrate intermediate work."""
_DETACHED_SUPERVISORS: dict[int, subprocess.Popen] = {}


def _filtered_executor_stderr(stderr: str) -> str:
    """Remove only known benign Codex model-cache diagnostics from stderr."""
    return "\n".join(
        line for line in stderr.splitlines() if not _CODEX_BENIGN_CACHE_STDERR.fullmatch(line)
    )


def _git(cwd: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def _git_root(cwd: Path) -> Path:
    result = _git(cwd, ["rev-parse", "--show-toplevel"])
    if result.returncode:
        raise HarnessError("delegation requires a Git repository")
    return Path(result.stdout.strip()).resolve()


def _dirty(cwd: Path) -> list[str]:
    result = _git(cwd, ["status", "--short", "--untracked-files=normal"])
    if result.returncode:
        raise HarnessError("could not inspect Git worktree")
    return [line for line in result.stdout.splitlines() if line]


def _delegated_changes_path(runtime_root: Path) -> Path:
    return runtime_root / "delegated-changes.json"


def _load_delegated_changes(runtime_root: Path) -> dict[str, dict[str, str]] | None:
    """Load the trusted delegated-change fingerprints, failing closed on bad data."""
    path = _delegated_changes_path(runtime_root)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    records: dict[str, dict[str, str]] = {}
    for root, changes in raw.items():
        if not isinstance(root, str) or not Path(root).is_absolute() or not isinstance(changes, dict):
            return None
        if not all(isinstance(name, str) and isinstance(fingerprint, str) for name, fingerprint in changes.items()):
            return None
        records[root] = changes
    return records


def _delegated_changes_match(
    runtime_root: Path, root: Path, dirty: list[str], current: list[dict]
) -> bool:
    records = _load_delegated_changes(runtime_root)
    if records is None:
        return False
    recorded = records.get(str(root.resolve()))
    if not isinstance(recorded, dict):
        return False
    # Diff details must account for every porcelain entry. This also rejects
    # submodules and other status entries for which we cannot fingerprint a file.
    if len(current) < len(dirty):
        return False
    current_fingerprints: dict[str, str] = {}
    for item in current:
        name = item.get("file")
        fingerprint = item.get("fingerprint")
        # Deleted paths and any un-fingerprintable entries are never trusted.
        if not isinstance(name, str) or not isinstance(fingerprint, str):
            return False
        current_fingerprints[name] = fingerprint
    return current_fingerprints == recorded


def _record_delegated_changes(
    runtime_root: Path, run_dir: Path, cwd: Path, current: list[dict], execution_delta: list[dict]
) -> None:
    """Record only this run's changes and still-current trusted changes."""
    records = _load_delegated_changes(runtime_root) or {}
    root = str(_git_root(cwd))
    current_fingerprints = {
        item["file"]: item["fingerprint"]
        for item in current
        if isinstance(item.get("file"), str) and isinstance(item.get("fingerprint"), str)
    }
    recorded = records.get(root, {})
    trusted = {
        name: fingerprint
        for name, fingerprint in recorded.items()
        if current_fingerprints.get(name) == fingerprint
    }
    generated = {
        item["file"]: item["fingerprint"]
        for item in execution_delta
        if isinstance(item.get("file"), str) and isinstance(item.get("fingerprint"), str)
    }
    records[root] = trusted | generated
    atomic_write(_delegated_changes_path(runtime_root), dump_json(records))


def _prepare_write_execution(
    config: dict,
    role_name: str,
    role: dict,
    kind: str,
    root: Path,
    runtime_root: Path,
    run_dir: Path,
    *,
    attempts: int = 0,
    thread_id: str | None = None,
    signatures: list[str] | None = None,
) -> Path:
    """Apply the shared write-worktree policy and return the execution root."""
    project = project_config(config, root)
    policy = project.get("dirty_worktree_policy", config["dirty_worktree_policy"])
    dirty = _dirty(root)
    if not role["write"] or not dirty:
        return root
    _, current, _ = _diff_details(root)
    allowed = policy == "allow_delegated" and _delegated_changes_match(runtime_root, root, dirty, current)
    if policy != "isolate" and not allowed:
        reason = "write delegation blocked by pre-existing changes:\n- " + "\n- ".join(dirty[:20])
        finalize_blocked_run(
            run_dir, role_name, role, kind, root, reason, "dirty_worktree",
            attempts=attempts, thread_id=thread_id, signatures=signatures,
        )
        raise DirtyWorktreeError(f"{reason}\nrun state: {run_dir}")
    if policy == "isolate":
        return _create_isolated_worktree(root, run_dir)
    return root


def _diff_details(cwd: Path) -> tuple[str, list[dict], list[str]]:
    stat_result = _git(cwd, ["diff", "HEAD", "--stat", "--", "."])
    if stat_result.returncode:
        stat_result = _git(cwd, ["diff", "--stat", "--", "."])
    numstat = _git(cwd, ["diff", "HEAD", "--numstat", "--no-renames", "--", "."])
    if numstat.returncode:
        numstat = _git(cwd, ["diff", "--numstat", "--no-renames", "--", "."])
    details: list[dict] = []
    changed: list[str] = []
    for line in numstat.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, file_name = parts
        file_path = cwd / file_name
        details.append({
            "file": file_name,
            "added": added,
            "deleted": deleted,
            "untracked": False,
            "fingerprint": sha256(file_path) if file_path.is_file() else None,
        })
        changed.append(file_name)
    untracked = _git(cwd, ["ls-files", "--others", "--exclude-standard", "-z"])
    for raw in untracked.stdout.split("\0"):
        if not raw:
            continue
        path = cwd / raw
        size = path.stat().st_size if path.is_file() else 0
        details.append({
            "file": raw,
            "bytes": size,
            "untracked": True,
            "fingerprint": sha256(path) if path.is_file() else None,
        })
        changed.append(raw)
    return stat_result.stdout, details, changed


def _write_baseline(run_dir: Path, cwd: Path) -> None:
    stat, details, changed = _diff_details(cwd)
    atomic_write(run_dir / "baseline.json", dump_json({
        "cwd": str(cwd),
        "diff_stat": stat,
        "diff_summary": details,
        "changed_files": changed,
    }))


def _write_execution_record(
    run_dir: Path,
    role_name: str,
    role: dict,
    kind: str,
    cwd: Path,
    parent_harness: str,
) -> None:
    """Record the runner-authorized execution identity before starting an executor."""
    atomic_write(run_dir / "execution.json", dump_json({
        "role_name": role_name,
        "harness": role["harness"],
        "parent_harness": parent_harness,
        "write": role["write"],
        "kind": kind,
        "cwd": str(cwd),
    }))


def _execution_delta(run_dir: Path, current: list[dict]) -> tuple[list[dict], set[str]]:
    baseline_path = run_dir / "baseline.json"
    if not baseline_path.exists():
        return current, set()
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return current, set()
    before = {
        item.get("file"): item
        for item in baseline.get("diff_summary", [])
        if isinstance(item, dict) and isinstance(item.get("file"), str)
    }
    after = {
        item.get("file"): item
        for item in current
        if isinstance(item, dict) and isinstance(item.get("file"), str)
    }
    delta = [item for name, item in after.items() if before.get(name) != item]
    for name, item in before.items():
        if name not in after:
            delta.append({
                "file": name,
                "untracked": item.get("untracked", False),
                "removed_preexisting_change": True,
            })
    return delta, set(before)


def _declared_checks(run_dir: Path) -> list[str]:
    """Read the bullet commands from the task file's Checks section."""
    path = run_dir / "task.md"
    if not path.exists():
        return []
    checks: list[str] = []
    in_checks = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _CHECKS_HEADING.match(line):
            in_checks = True
            continue
        if in_checks and _HEADING.match(line):
            break
        if not in_checks:
            continue
        item = _CHECK_ITEM.match(line)
        if not item:
            continue
        check = item.group(1).strip()
        if len(check) >= 2 and check.startswith("`") and check.endswith("`"):
            check = check[1:-1].strip()
        if check:
            checks.append(check)
    return checks


def _check_results(checks: list[str], executions: list[dict]) -> list[dict]:
    """Evaluate each check from its last non-policy-denied matching execution."""
    results: list[dict] = []
    for check in checks:
        matching = [
            execution for execution in executions
            if not execution.get("policy_denied")
            and command_matches_check(str(execution.get("command", "")), check)
        ]
        if not matching:
            results.append({"check": check, "status": "not_run", "exit_code": None})
            continue
        last = matching[-1]
        exit_code = last.get("exit_code")
        results.append({
            "check": check,
            "status": "passed" if exit_code == 0 else "not_run" if exit_code is None else "failed",
            "exit_code": exit_code,
        })
    return results


def _tracked_path(
    cwd: Path,
    value: str,
    tracked_basenames: dict[str, set[str]] | None = None,
) -> str | None:
    path = value.strip().strip("'\"")
    if not path or path.startswith("-") or path == "/dev/null":
        return None
    result = _git(cwd, ["ls-files", "--error-unmatch", "--", path])
    if result.returncode == 0:
        return path
    basename = Path(path).name
    if not basename:
        return None
    if tracked_basenames is None:
        tracked = _git(cwd, ["ls-files"])
        basenames = {Path(candidate).name for candidate in tracked.stdout.splitlines()}
    else:
        if "values" not in tracked_basenames:
            tracked = _git(cwd, ["ls-files"])
            tracked_basenames["values"] = {
                Path(candidate).name for candidate in tracked.stdout.splitlines()
            }
        basenames = tracked_basenames["values"]
    if basename in basenames:
        return path
    return None


def _self_reversions(cwd: Path, executions: list[dict]) -> list[dict]:
    """Return successful Git commands that can restore tracked worktree state."""
    reversions: list[dict] = []
    tracked_basenames: dict[str, set[str]] = {}
    for execution in executions:
        if execution.get("policy_denied") or execution.get("exit_code") not in {None, 0}:
            continue
        command = str(execution.get("command", ""))
        targets: list[tuple[str, str]] = []
        for match in _GIT_SHOW_HEAD_REDIRECT.finditer(command):
            target = _tracked_path(cwd, match.group(1), tracked_basenames)
            if target:
                targets.append(("git show HEAD redirect", target))
        for match in _GIT_CHECKOUT_PATHSPEC.finditer(command):
            for value in match.group(1).split():
                target = _tracked_path(cwd, value, tracked_basenames)
                if target:
                    targets.append(("git checkout", target))
        for match in _GIT_RESTORE.finditer(command):
            values = match.group(1)
            if "--" in values:
                values = values.split("--", 1)[1]
            for value in values.split():
                target = _tracked_path(cwd, value, tracked_basenames)
                if target:
                    targets.append(("git restore", target))
        for match in _GIT_STASH.finditer(command):
            values = match.group(1)
            if "--" in values:
                for value in values.split("--", 1)[1].split():
                    target = _tracked_path(cwd, value, tracked_basenames)
                    if target:
                        targets.append(("git stash", target))
            elif re.match(r"\s*(?:$|push\b|save\b)", values):
                targets.append(("git stash", "tracked worktree changes"))
        for source, target in targets:
            reversion = {"command": command, "source": source, "target": target}
            if reversion not in reversions:
                reversions.append(reversion)
    return reversions


def _create_isolated_worktree(root: Path, run_dir: Path) -> Path:
    worktree = run_dir / "worktree"
    result = _git(root, ["worktree", "add", "--detach", str(worktree), "HEAD"], timeout=60)
    if result.returncode:
        raise HarnessError(f"could not create isolated worktree: {result.stderr.strip()}")
    atomic_write(run_dir / "ISOLATED_WORKTREE", str(worktree) + "\n")
    return worktree


def _new_run_dir(runtime_root: Path) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    run_dir = runtime_root / "runs" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, mode=0o700)
    return run_dir


def _tee(pipe, target: Path) -> None:
    with target.open("wb") as handle:
        while True:
            block = pipe.read1(65536)
            if not block:
                break
            handle.write(block)
            handle.flush()


def _invoke_safe(command: list[str], task: str, env: dict[str, str], cwd: Path, run_dir: Path, timeout: int) -> int:
    """Separate wrapper keeps exception binding correct on Python 3.11+."""
    try:
        return _invoke_inner(command, task, env, cwd, run_dir, timeout)
    except KeyboardInterrupt:
        atomic_write(run_dir / "INTERRUPTED", "interrupt\n")
        return 130


def _invoke_inner(command: list[str], task: str, env: dict[str, str], cwd: Path, run_dir: Path, timeout: int) -> int:
    events = run_dir / "events.jsonl"
    stderr = run_dir / "stderr.log"
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    threads = [
        threading.Thread(target=_tee, args=(process.stdout, events), daemon=True),
        threading.Thread(target=_tee, args=(process.stderr, stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        assert process.stdin is not None
        process.stdin.write(task.encode("utf-8"))
        process.stdin.close()
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            atomic_write(run_dir / "INTERRUPTED", "timeout\n")
            return 124
    except KeyboardInterrupt:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
        raise
    finally:
        for thread in threads:
            thread.join(timeout=10)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _delegation_schema() -> Path:
    schema = source_root() / "schemas/delegation-result.schema.json"
    if not schema.exists():
        schema = user_paths().install_root / "schemas/delegation-result.schema.json"
    return schema


def _codex_command(codex: Path, role: dict, cwd: Path, run_dir: Path, resume: str | None = None) -> list[str]:
    schema = _delegation_schema()
    base = [str(codex), "exec"]
    sandbox = "workspace-write" if role["write"] else "read-only"
    if resume:
        return [
            *base, "resume", resume,
            "--json",
            "-m", role["model"],
            "-c", f'sandbox_mode="{sandbox}"',
            "-c", f'model_reasoning_effort="{role["effort"]}"',
            "-c", 'model_provider="openai"',
            "-c", 'forced_login_method="chatgpt"',
            "-c", 'shell_environment_policy.inherit="core"',
            "--output-schema", str(schema),
            "-o", str(run_dir / "final.json"), "-",
        ]
    return [
        *base, "--json", "--sandbox", sandbox, "-C", str(cwd),
        "-m", role["model"],
        "-c", f'model_reasoning_effort="{role["effort"]}"',
        "-c", 'model_provider="openai"',
        "-c", 'forced_login_method="chatgpt"',
        "-c", 'shell_environment_policy.inherit="core"',
        "--output-schema", str(schema),
        "-o", str(run_dir / "final.json"), "-",
    ]


def _claude_command(
    claude: Path,
    role_name: str,
    role: dict,
    cwd: Path,
    run_dir: Path,
    claude_agents: Path,
    resume: str | None = None,
) -> list[str]:
    schema = _delegation_schema()
    claude_schema = json.loads(schema.read_text(encoding="utf-8"))
    claude_schema.pop("$schema", None)
    allowed_tools = CLAUDE_INSPECTION_TOOLS
    if role["write"]:
        execution_root = cwd.resolve()
        scope = f"//{execution_root.as_posix().lstrip('/')}/**"
        allowed_tools = f"{allowed_tools},Edit({scope}),Write({scope})"
    agent_path = claude_agents / f"cross-harness-{role_name}.md"
    agent_instruction = ""
    if agent_path.is_file():
        agent_instruction = agent_path.read_text(encoding="utf-8")
        agent_instruction = re.sub(
            r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)",
            "",
            agent_instruction,
            count=1,
            flags=re.DOTALL,
        ).strip()
    result_instruction = (
        CLAUDE_EXECUTOR_CHARTER
        + "\n\n"
        + (agent_instruction + "\n\n" if agent_instruction else "")
        + "When the task is complete, respond with only a JSON object conforming to "
        f"{schema}. It must contain exactly: status (one of success, failed, blocked, "
        "partial), work_completed (string), changed_files (array of strings), tests "
        "(array of strings), error (string or null), and next_decision (string or null). "
        "Your entire final message must be that JSON object: do not include "
        "explanatory prose or Markdown code fences before or after it. Do not write the "
        "result to a file. "
        "Do not include credentials or authentication material."
    )
    command = [
        str(claude), "-p",
        *(["--resume", resume] if resume else []),
        "--model", role["model"],
        "--effort", role["effort"],
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "manual",
        "--allowedTools", allowed_tools,
        *([] if role["write"] else ["--disallowed-tools", "Edit", "Write", "NotebookEdit"]),
        "--json-schema", json.dumps(claude_schema, ensure_ascii=False, separators=(",", ":")),
        "--append-system-prompt", result_instruction,
    ]
    return command


def _sandbox_profile_string(value: str | Path) -> str:
    """Return a safely quoted Sandbox Profile Language string literal."""
    # SBPL uses backslash escaping in string literals.  Escape it before quotes
    # so a path cannot terminate a literal or add a new profile form.
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def _claude_sandbox_profile(execution_root: Path, home: Path) -> str:
    """Build the write confinement profile for a Claude executor."""
    writable_subpaths = (
        execution_root.resolve(),
        (home / ".claude").resolve(),
        (home / ".cache").resolve(),
        (home / "Library/Caches").resolve(),
        Path("/private/tmp"),
        Path("/private/var/tmp"),
        Path("/private/var/folders"),
    )
    writable_devices = (
        "/dev/null",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/dtracehelper",
        "/dev/tty",
    )
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
    ]
    rules.extend(
        f"(allow file-write* (subpath {_sandbox_profile_string(path)}))"
        for path in writable_subpaths
    )
    rules.extend(
        f"(allow file-write-data (literal {_sandbox_profile_string(path)}))"
        for path in writable_devices
    )
    return "\n".join(rules) + "\n"


def _contain_claude_write_command(
    command: list[str], role: dict, execution_root: Path, run_dir: Path, home: Path
) -> tuple[list[str], dict[str, object]]:
    """Wrap a writable Claude executor in sandbox-exec when it is available."""
    disabled = {"enabled": False, "profile": None}
    if role["harness"] != "claude" or not role["write"]:
        return command, disabled | {"reason": "not_writable_claude"}
    if sys.platform != "darwin":
        return command, disabled | {"reason": "platform_not_darwin"}
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        return command, disabled | {"reason": "sandbox_exec_unavailable"}
    profile_path = run_dir / "sandbox-exec.sb"
    atomic_write(profile_path, _claude_sandbox_profile(execution_root, home))
    return (
        [sandbox_exec, "-f", str(profile_path), *command],
        {"enabled": True, "profile": str(profile_path), "tool": sandbox_exec},
    )


def _command(
    executor: Path,
    role_name: str,
    role: dict,
    cwd: Path,
    run_dir: Path,
    claude_agents: Path,
    resume: str | None = None,
) -> list[str]:
    if role["harness"] == "codex":
        return _codex_command(executor, role, cwd, run_dir, resume)
    if role["harness"] == "claude":
        return _claude_command(executor, role_name, role, cwd, run_dir, claude_agents, resume)
    raise ConfigError(f"unsupported harness: {role['harness']}")


def _write_claude_final_from_events(run_dir: Path) -> None:
    """Persist Claude's final result event as JSON, or its raw text as a fallback."""
    final_path = run_dir / "final.json"
    final_text_path = run_dir / "final.txt"
    events_path = run_dir / "events.jsonl"
    if final_path.exists() or not events_path.exists():
        return
    result_text: str | None = None
    with events_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                value = event.get("result")
                result_text = value if isinstance(value, str) else None
    if result_text is None:
        return
    result_body = result_text
    result_text = result_text.strip()
    fenced = re.fullmatch(r"```[^\r\n]*\r?\n(.*?)\r?\n?```", result_text, flags=re.DOTALL)
    if fenced:
        result_text = fenced.group(1).strip()
    else:
        # Claude can prepend prose even when its final response is a fenced JSON
        # object. Only recover a trailing fence after rejecting the entire result
        # as JSON, preserving the existing handling for complete fenced messages.
        try:
            json.loads(result_text)
        except json.JSONDecodeError:
            trailing_fence = re.search(
                r"```[^\r\n]*\r?\n(.*?)\r?\n?```\s*\Z", result_text, flags=re.DOTALL
            )
            if trailing_fence:
                result_text = trailing_fence.group(1).strip()
    try:
        final = json.loads(result_text)
    except json.JSONDecodeError:
        atomic_write(final_text_path, result_body)
        return
    if isinstance(final, dict):
        atomic_write(final_path, dump_json(final))
    else:
        atomic_write(final_text_path, result_body)


def _final_message_path(run_dir: Path) -> str | None:
    """Return the existing final response artifact, preferring structured output."""
    for name in ("final.json", "final.txt"):
        path = run_dir / name
        if path.is_file():
            return str(path)
    return None


def delegate(
    role_name: str,
    kind: str,
    task_file: Path,
    cwd: Path,
    config_path: Path | None = None,
    home: Path | None = None,
    confirm_high_risk: bool = False,
    run_dir: Path | None = None,
) -> dict:
    if os.environ.get("CROSS_HARNESS_ACTIVE") == "1":
        raise HarnessError("nested cross-harness delegation from an active executor is blocked")
    paths = user_paths(home)
    config = load_config(config_path, paths.home)
    if role_name not in config["roles"]:
        raise ConfigError(f"unknown role: {role_name}")
    role = dict(config["roles"][role_name])
    if kind not in config["delegate_kinds"] or kind not in role["delegate_kinds"]:
        raise ConfigError(delegation_kind_error(
            kind, role_name, config["delegate_kinds"], role["delegate_kinds"]
        ))
    if role_name == "security_reviewer" and not confirm_high_risk:
        raise HarnessError("security review requires --confirm-high-risk after explicit human confirmation")
    if not task_file.is_file():
        raise HarnessError(f"task file not found: {task_file}")
    if task_file.name.lower() in {"auth.json", ".env", "credentials.json"}:
        raise HarnessError("credential or environment files cannot be used as task files")
    task = task_file.read_text(encoding="utf-8")
    if not task.strip():
        raise HarnessError("task file is empty")
    if contains_secret(task):
        raise HarnessError("task file appears to contain credential material; refusing delegation")
    root = _git_root(cwd)
    runtime_root = Path(config["runtime_root"])
    run_dir = run_dir or _new_run_dir(runtime_root)
    if not run_dir.is_dir():
        raise HarnessError(f"run directory not found: {run_dir}")
    run_task = run_dir / "task.md"
    if task_file.resolve() != run_task.resolve():
        shutil.copy2(task_file, run_task)
    execution_root = _prepare_write_execution(
        config, role_name, role, kind, root, runtime_root, run_dir,
    )
    _write_baseline(run_dir, execution_root)
    try:
        if role["harness"] == "codex":
            verify_codex_config_ownership(paths.home, root, execution_root)
            executor, cached = verify_codex_chatgpt(runtime_root, paths.home, config["auth_cache_hours"])
        elif role["harness"] == "claude":
            verify_claude_config_ownership(paths.home, root)
            executor, cached = verify_claude_subscription(runtime_root, paths.home, config["auth_cache_hours"])
        else:
            raise ConfigError(f"unsupported harness: {role['harness']}")
    except AuthError as exc:
        return finalize_blocked_run(run_dir, role_name, role, kind, execution_root, str(exc), "authentication")
    environment = sanitized_environment(paths.home, {
        "CROSS_HARNESS_ACTIVE": "1",
        "CROSS_HARNESS_EXECUTOR": role["harness"],
        "CROSS_HARNESS_PARENT": config["parent_harness"],
        "CROSS_HARNESS_RUN_DIR": str(run_dir),
    })
    if role["write"]:
        environment["CROSS_HARNESS_WRITE"] = "1"
    else:
        environment.pop("CROSS_HARNESS_WRITE", None)
    command = _command(executor, role_name, role, execution_root, run_dir, paths.claude / "agents")
    command, sandbox_exec = _contain_claude_write_command(
        command, role, execution_root, run_dir, paths.home
    )
    _write_execution_record(
        run_dir, role_name, role, kind, execution_root, config["parent_harness"]
    )
    atomic_write(run_dir / "command.json", dump_json({
        "argv": command,
        "auth_cached": cached,
        "cwd": str(execution_root),
        "sandbox_exec": sandbox_exec,
    }))
    exit_code = _invoke_safe(command, task, environment, execution_root, run_dir, role["timeout_seconds"])
    if role["harness"] == "claude":
        _write_claude_final_from_events(run_dir)
    return finalize_run(
        run_dir, role_name, role, kind, execution_root, exit_code, attempt=1,
        runtime_root=runtime_root,
    )


def start_detached_delegate(
    role_name: str,
    kind: str,
    task_file: Path,
    cwd: Path,
    config_path: Path | None = None,
    home: Path | None = None,
    confirm_high_risk: bool = False,
) -> Path:
    """Create a run and start a detached CLI supervisor for it."""
    if os.environ.get("CROSS_HARNESS_ACTIVE") == "1":
        raise HarnessError("nested cross-harness delegation from an active executor is blocked")
    paths = user_paths(home)
    config = load_config(config_path, paths.home)
    if role_name not in config["roles"]:
        raise ConfigError(f"unknown role: {role_name}")
    role = config["roles"][role_name]
    if kind not in config["delegate_kinds"] or kind not in role["delegate_kinds"]:
        raise ConfigError(delegation_kind_error(
            kind, role_name, config["delegate_kinds"], role["delegate_kinds"]
        ))
    if role_name == "security_reviewer" and not confirm_high_risk:
        raise HarnessError("security review requires --confirm-high-risk after explicit human confirmation")
    if not task_file.is_file():
        raise HarnessError(f"task file not found: {task_file}")
    if task_file.name.lower() in {"auth.json", ".env", "credentials.json"}:
        raise HarnessError("credential or environment files cannot be used as task files")
    task = task_file.read_text(encoding="utf-8")
    if not task.strip():
        raise HarnessError("task file is empty")
    if contains_secret(task):
        raise HarnessError("task file appears to contain credential material; refusing delegation")
    _git_root(cwd)
    run_dir = _new_run_dir(Path(config["runtime_root"]))
    run_task = run_dir / "task.md"
    shutil.copy2(task_file, run_task)
    command = [sys.executable, "-m", "cross_harness.cli"]
    if home is not None:
        command.extend(["--home", str(home)])
    command.extend([
        "delegate",
        "--role", role_name,
        "--kind", kind,
        "--task-file", str(run_task),
        "--cwd", str(cwd),
        "--run-dir", str(run_dir),
        "--supervisor",
    ])
    if config_path is not None:
        command.extend(["--config", str(config_path)])
    if confirm_high_risk:
        command.append("--confirm-high-risk")
    environment = os.environ.copy()
    environment.pop("CROSS_HARNESS_ACTIVE", None)
    package_root = str(Path(__file__).resolve().parents[1])
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (package_root, existing_python_path) if path
    )
    with (
        (run_dir / "supervisor.stdin").open("w+b") as stdin,
        (run_dir / "supervisor.stdout.log").open("wb") as stdout,
        (run_dir / "supervisor.stderr.log").open("wb") as stderr,
    ):
        process = subprocess.Popen(
            command,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    atomic_write(run_dir / "supervisor.pid", f"{process.pid}\n")
    _DETACHED_SUPERVISORS[process.pid] = process
    return run_dir


def _supervisor_pid(run_dir: Path) -> int | None:
    try:
        pid = int((run_dir / "supervisor.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _supervisor_alive(run_dir: Path) -> bool:
    """Return whether a recorded supervisor is running, reaping it if possible."""
    pid = _supervisor_pid(run_dir)
    if pid is None:
        return False
    process = _DETACHED_SUPERVISORS.get(pid)
    if process is not None:
        if process.poll() is not None:
            del _DETACHED_SUPERVISORS[pid]
            return False
        return True
    if _reap_supervisor_pid(pid):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if status.returncode:
        return False
    if status.stdout.lstrip().startswith("Z"):
        _reap_supervisor_pid(pid)
        return False
    return True


def _reap_supervisor_pid(pid: int) -> bool:
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    if reaped != pid:
        return False
    _DETACHED_SUPERVISORS.pop(pid, None)
    return True


def _reap_supervisor(run_dir: Path) -> None:
    pid = _supervisor_pid(run_dir)
    if pid is None:
        return
    process = _DETACHED_SUPERVISORS.get(pid)
    if process is None:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        return
    del _DETACHED_SUPERVISORS[pid]


def _completed_summary(run_dir: Path) -> dict | None:
    summary_path = run_dir / "summary.json"
    if not summary_path.is_file() or not (run_dir / "summary.txt").is_file():
        return None
    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Atomic writes make this unlikely, but a concurrent finalizer is harmless.
        return None


def wait_for_run(run_dir: Path, timeout_seconds: float, poll_seconds: float = 0.1) -> dict | None:
    """Return a completed summary, None at timeout, or fail if its supervisor died."""
    if timeout_seconds < 0:
        raise HarnessError("timeout seconds must be non-negative")
    deadline = time.monotonic() + timeout_seconds
    while True:
        summary = _completed_summary(run_dir)
        if summary is not None:
            _reap_supervisor(run_dir)
            return summary
        if (run_dir / "supervisor.pid").is_file() and not _supervisor_alive(run_dir):
            summary = _completed_summary(run_dir)
            if summary is not None:
                _reap_supervisor(run_dir)
                return summary
            raise SupervisorDiedError("delegation supervisor exited without producing a summary")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_seconds, remaining))


def finalize_blocked_run(
    run_dir: Path,
    role_name: str,
    role: dict,
    kind: str,
    cwd: Path,
    reason: str,
    category: str,
    attempts: int = 0,
    thread_id: str | None = None,
    signatures: list[str] | None = None,
) -> dict:
    final = {
        "status": "blocked",
        "work_completed": "Executor was not started.",
        "changed_files": [],
        "tests": [],
        "error": reason,
        "next_decision": "Resolve the blocking condition and start a new delegation.",
    }
    atomic_write(run_dir / "events.jsonl", "")
    atomic_write(run_dir / "stderr.log", reason + "\n")
    atomic_write(run_dir / "final.json", dump_json(final))
    atomic_write(run_dir / "diff-stat.txt", "")
    summary = {
        "status": "blocked",
        "run_dir": str(run_dir),
        "exit_code": None,
        "role": role_name,
        "kind": kind,
        "model": role["model"],
        "effort": role["effort"],
        "attempt": attempts,
        "thread_id": thread_id,
        "changed_files": [],
        "tests": [],
        "work_completed": final["work_completed"],
        "error": reason,
        "next_decision": final["next_decision"],
        "usage": {},
        "failure_signature": None,
        "event_log": str(run_dir / "events.jsonl"),
        "stderr_log": str(run_dir / "stderr.log"),
        "final_message": _final_message_path(run_dir),
        "cwd": str(cwd),
    }
    state = {
        "role": role_name,
        "kind": kind,
        "cwd": str(cwd),
        "thread_id": thread_id,
        "attempts": attempts,
        "signatures": signatures or [],
        "escalated": False,
        "status": "blocked",
        "blocked_category": category,
        "blocked_reason": reason,
        "model": role["model"],
        "effort": role["effort"],
    }
    atomic_write(run_dir / "summary.json", dump_json(summary))
    atomic_write(run_dir / "state.json", dump_json(state))
    atomic_write(run_dir / "BLOCKED", f"{category}: {reason}\n")
    atomic_write(run_dir / "summary.txt", render_summary(summary, role["output_limit_chars"]))
    return summary


def finalize_run(
    run_dir: Path,
    role_name: str,
    role: dict,
    kind: str,
    cwd: Path,
    exit_code: int,
    attempt: int,
    *,
    runtime_root: Path | None = None,
) -> dict:
    parsed = parse_events(run_dir / "events.jsonl")
    declared_checks = _declared_checks(run_dir)
    check_results = _check_results(declared_checks, parsed.get("executions", []))
    unrelated_failed_command_count = sum(
        1
        for execution in parsed.get("executions", [])
        if not execution.get("policy_denied")
        and execution.get("exit_code") not in {None, 0}
        and not any(command_matches_check(str(execution.get("command", "")), check) for check in declared_checks)
    )
    self_reversions: list[dict] = []
    self_reversion_check_unavailable = False
    if role.get("write"):
        try:
            self_reversions = _self_reversions(cwd, parsed.get("executions", []))
        except (subprocess.TimeoutExpired, OSError):
            self_reversion_check_unavailable = True
    final = load_final(run_dir / "final.json") or {}
    stderr = (run_dir / "stderr.log").read_text(encoding="utf-8", errors="replace") if (run_dir / "stderr.log").exists() else ""
    filtered_stderr = _filtered_executor_stderr(stderr)
    event_failed = bool(parsed.get("errors"))
    allowed_statuses = {"success", "failed", "blocked", "partial"}
    reported_status = final.get("status")
    invalid_reported_status = "status" in final and reported_status not in allowed_statuses
    if invalid_reported_status:
        status = "failed"
        status_error = f"invalid final status {reported_status!r}; expected one of {sorted(allowed_statuses)}"
    elif reported_status in allowed_statuses:
        status = reported_status
        status_error = ""
    else:
        status = "success" if exit_code == 0 and not event_failed else "failed"
        status_error = ""
    if exit_code != 0 and status == "success":
        status = "failed"
    if event_failed and status == "success":
        status = "failed"
    command_error = ""
    reversion_error = ""
    failed_checks = [item for item in check_results if item["status"] == "failed"]
    unrun_checks = [item for item in check_results if item["status"] == "not_run"]
    if failed_checks and status == "success":
        status = "failed"
        command_error = "declared check failed: " + "; ".join(
            f"{item['check']} (exit {item['exit_code']})" for item in failed_checks
        )
    elif unrun_checks and status == "success":
        status = "partial"
        command_error = "declared check not run: " + "; ".join(item["check"] for item in unrun_checks)
    if not declared_checks and kind in {"test", "implementation", "debug"} and status == "success":
        status = "partial"
        command_error = (
            "no checks declared; outcome could not be verified. "
            "Create the task with an executable command line passed to task create --check."
        )
    if self_reversions:
        reversion_error = "tracked files restored from Git: " + "; ".join(
            item["target"] for item in self_reversions
        )
        command_error = "\n".join(item for item in (command_error, reversion_error) if item)
        if status == "success":
            status = "partial"
    executor_error = str(final.get("error") or "\n".join(parsed.get("errors", [])[-3:]) or "")
    if status != "success":
        executor_error = executor_error or filtered_stderr[-2000:]
    combined_error = "\n".join(error for error in (status_error, command_error, executor_error) if error)
    blocked_category = parsed.get("blocked_category")
    if blocked_category == "rate_limit":
        status = "blocked"
        combined_error = "rate limit detected; no fallback or automatic waiting was attempted"
    elif blocked_category == "authentication":
        status = "blocked"
        combined_error = "authentication failure detected; no billing fallback was attempted"
    elif RATE_LIMIT.search("\n".join((combined_error, filtered_stderr))):
        status = "blocked"
        blocked_category = "rate_limit"
        combined_error = "rate limit detected; no fallback or automatic waiting was attempted"
    elif AUTH_FAILURE.search("\n".join((combined_error, filtered_stderr))):
        status = "blocked"
        blocked_category = "authentication"
        combined_error = "authentication failure detected; no billing fallback was attempted"
    elif status == "blocked":
        blocked_category = "executor_reported"
    if reversion_error and reversion_error not in combined_error:
        combined_error = f"{combined_error}\n{reversion_error}".strip()
    diff_check_unavailable = False
    try:
        diff_stat, current_diff_summary, _ = _diff_details(cwd)
    except (subprocess.TimeoutExpired, OSError):
        diff_check_unavailable = True
        diff_stat = ""
        current_diff_summary = []
        diff_summary = []
        baseline_names: set[str] = set()
    else:
        diff_summary, baseline_names = _execution_delta(run_dir, current_diff_summary)
        if not diff_summary:
            diff_stat = ""
    detected_changed = [item["file"] for item in diff_summary]
    if role.get("write") is False and diff_summary and status != "blocked":
        status = "failed"
        readonly_error = "read-only role modified the worktree"
        combined_error = f"{combined_error}\n{readonly_error}".strip()
    should_record_delegated_changes = (
        runtime_root is not None
        and role.get("write")
        and status == "success"
        and not (run_dir / "ISOLATED_WORKTREE").exists()
    )
    if should_record_delegated_changes and not diff_check_unavailable:
        try:
            at_git_root = cwd.resolve() == _git_root(cwd)
            if at_git_root:
                _record_delegated_changes(runtime_root, run_dir, cwd, current_diff_summary, diff_summary)
        except (subprocess.TimeoutExpired, OSError):
            # A failed root lookup or delegated-change fingerprint cannot be
            # trusted as a diff result, so do not report inferred changes.
            diff_check_unavailable = True
            diff_stat = ""
            current_diff_summary = []
            diff_summary = []
            detected_changed = []
            baseline_names = set()
    atomic_write(run_dir / "diff-stat.txt", diff_stat)
    reported_changed = final.get("changed_files") if isinstance(final.get("changed_files"), list) else []
    reported_changed = [summary_item_text(name) for name in reported_changed]
    filtered_reported = [name for name in reported_changed if name not in baseline_names or name in detected_changed]
    reported_changed_files = list(dict.fromkeys(filtered_reported))
    unverified_changed_files = [
        name for name in reported_changed_files if name not in detected_changed
    ]
    signature = failure_signature(exit_code, parsed, filtered_stderr)
    reported_tests = final.get("tests", [])
    if isinstance(reported_tests, str):
        reported_tests = [reported_tests]
    elif not isinstance(reported_tests, list):
        reported_tests = []
    else:
        reported_tests = [summary_item_text(test) for test in reported_tests]
    summary = {
        "status": status,
        "run_dir": str(run_dir),
        "exit_code": exit_code,
        "role": role_name,
        "kind": kind,
        "model": role["model"],
        "effort": role["effort"],
        "attempt": attempt,
        "thread_id": parsed.get("thread_id"),
        "changed_files": detected_changed,
        "reported_changed_files": reported_changed_files,
        "unverified_changed_files": unverified_changed_files,
        "diff_summary": diff_summary,
        "tests": reported_tests,
        "checks": check_results,
        "unrelated_failed_command_count": unrelated_failed_command_count,
        "self_reversions": self_reversions,
        "work_completed": str(final.get("work_completed", "")),
        "error": combined_error[:4000] or None,
        "next_decision": final.get("next_decision"),
        "usage": parsed.get("usage", {}),
        "failure_signature": signature,
        "event_log": str(run_dir / "events.jsonl"),
        "stderr_log": str(run_dir / "stderr.log"),
        "final_message": _final_message_path(run_dir),
        "diff_stat_file": str(run_dir / "diff-stat.txt"),
        "baseline_file": str(run_dir / "baseline.json"),
        "cwd": str(cwd),
    }
    if self_reversion_check_unavailable:
        summary["self_reversion_check"] = "unavailable"
    if diff_check_unavailable:
        summary["diff_check"] = "unavailable"
    state = {
        "role": role_name,
        "kind": kind,
        "cwd": str(cwd),
        "thread_id": parsed.get("thread_id"),
        "attempts": attempt,
        "signatures": [signature] if signature else [],
        "escalated": False,
        "status": status,
        "model": role["model"],
        "effort": role["effort"],
    }
    if blocked_category:
        state["blocked_category"] = blocked_category
        state["blocked_reason"] = combined_error
    raw_paths = (
        run_dir / "events.jsonl", run_dir / "stderr.log", run_dir / "final.json", run_dir / "final.txt",
    )
    summary["raw_artifact_bytes"] = sum(path.stat().st_size for path in raw_paths if path.exists())
    summary["summary_bytes"] = 0
    summary["compression_percent"] = 0.0
    summary_text = ""
    for _ in range(4):
        summary_text = render_summary(summary, role["output_limit_chars"])
        byte_count = len(summary_text.encode("utf-8"))
        percent = (
            (1 - byte_count / summary["raw_artifact_bytes"]) * 100
            if summary["raw_artifact_bytes"]
            else 0.0
        )
        if byte_count == summary["summary_bytes"] and abs(percent - summary["compression_percent"]) < 0.01:
            break
        summary["summary_bytes"] = byte_count
        summary["compression_percent"] = percent
    atomic_write(run_dir / "summary.json", dump_json(summary))
    atomic_write(run_dir / "state.json", dump_json(state))
    if blocked_category:
        atomic_write(run_dir / "BLOCKED", f"{blocked_category}: {combined_error}\n")
    atomic_write(run_dir / "summary.txt", summary_text)
    return summary


def _escalated_role(role: dict, config: dict) -> dict:
    updated = dict(role)
    harness = role["harness"]
    chain = config["fallback"][harness]
    if role["model"] in chain:
        index = chain.index(role["model"])
        if index > 0:
            updated["model"] = chain[index - 1]
    efforts = CODEX_EFFORTS if harness == "codex" else CLAUDE_EFFORTS
    if role["effort"] in efforts and efforts.index(role["effort"]) < len(efforts) - 1:
        updated["effort"] = efforts[efforts.index(role["effort"]) + 1]
    return updated


def retry(run_dir: Path, task_file: Path, config_path: Path | None = None, home: Path | None = None) -> dict:
    if os.environ.get("CROSS_HARNESS_ACTIVE") == "1":
        raise HarnessError("nested cross-harness retry from an active executor is blocked")
    paths = user_paths(home)
    config = load_config(config_path, paths.home)
    state_path = run_dir / "state.json"
    if not state_path.exists():
        raise HarnessError(f"run state not found: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    role_name = state["role"]
    role = dict(config["roles"][role_name])
    if state["status"] == "blocked":
        raise HarnessError("blocked runs cannot be retried automatically")
    if state["attempts"] > role["retries"]:
        raise HarnessError("normal retry budget exhausted")
    if not task_file.is_file():
        raise HarnessError(f"task file not found: {task_file}")
    if task_file.name.lower() in {"auth.json", ".env", "credentials.json"}:
        raise HarnessError("credential or environment files cannot be used as task files")
    task = task_file.read_text(encoding="utf-8")
    if not task.strip():
        raise HarnessError("task file is empty")
    if contains_secret(task):
        raise HarnessError("task file appears to contain credential material; refusing delegation")
    runtime_root = Path(config["runtime_root"])
    source_root = _git_root(Path(state["cwd"]))
    retry_root = _new_run_dir(runtime_root)
    shutil.copy2(task_file, retry_root / "task.md")
    execution_root = _prepare_write_execution(
        config, role_name, role, state["kind"], source_root, runtime_root, retry_root,
        attempts=state["attempts"], thread_id=state["thread_id"], signatures=state.get("signatures", []),
    )
    _write_baseline(retry_root, execution_root)
    try:
        if role["harness"] == "codex":
            verify_codex_config_ownership(paths.home, source_root, execution_root)
            executor, cached = verify_codex_chatgpt(runtime_root, paths.home, config["auth_cache_hours"])
        elif role["harness"] == "claude":
            verify_claude_config_ownership(paths.home, source_root)
            executor, cached = verify_claude_subscription(runtime_root, paths.home, config["auth_cache_hours"])
        else:
            raise ConfigError(f"unsupported harness: {role['harness']}")
    except AuthError as exc:
        return finalize_blocked_run(
            retry_root,
            state["role"],
            role,
            state["kind"],
            execution_root,
            str(exc),
            "authentication",
            attempts=state["attempts"],
            thread_id=state["thread_id"],
            signatures=state.get("signatures", []),
        )
    environment = sanitized_environment(paths.home, {
        "CROSS_HARNESS_ACTIVE": "1",
        "CROSS_HARNESS_EXECUTOR": role["harness"],
        "CROSS_HARNESS_PARENT": config["parent_harness"],
        "CROSS_HARNESS_RUN_DIR": str(retry_root),
    })
    if role["write"]:
        environment["CROSS_HARNESS_WRITE"] = "1"
    else:
        environment.pop("CROSS_HARNESS_WRITE", None)
    command = _command(
        executor, role_name, role, execution_root, retry_root, paths.claude / "agents", state["thread_id"]
    )
    command, sandbox_exec = _contain_claude_write_command(
        command, role, execution_root, retry_root, paths.home
    )
    _write_execution_record(
        retry_root,
        state["role"],
        role,
        state["kind"],
        execution_root,
        config["parent_harness"],
    )
    atomic_write(retry_root / "command.json", dump_json({
        "argv": command,
        "auth_cached": cached,
        "resume": state["thread_id"],
        "sandbox_exec": sandbox_exec,
    }))
    exit_code = _invoke_safe(command, task, environment, execution_root, retry_root, role["timeout_seconds"])
    if role["harness"] == "claude":
        _write_claude_final_from_events(retry_root)
    summary = finalize_run(
        retry_root, state["role"], role, state["kind"], execution_root, exit_code, state["attempts"] + 1,
        runtime_root=runtime_root,
    )
    new_state = json.loads((retry_root / "state.json").read_text(encoding="utf-8"))
    new_state["signatures"] = [*state.get("signatures", []), *new_state.get("signatures", [])]
    identical = summary.get("failure_signature") and new_state["signatures"].count(summary["failure_signature"]) >= 2
    if summary["status"] == "failed" and identical and not state.get("escalated"):
        new_state["escalated"] = True
        atomic_write(retry_root / "state.json", dump_json(new_state))
        escalation = _escalated_role(role, config)
        escalation_root = _new_run_dir(runtime_root)
        shutil.copy2(task_file, escalation_root / "task.md")
        escalation_execution_root = _prepare_write_execution(
            config, role_name, escalation, state["kind"], source_root, runtime_root, escalation_root,
            attempts=new_state["attempts"], thread_id=summary.get("thread_id"),
            signatures=new_state.get("signatures", []),
        )
        _write_baseline(escalation_root, escalation_execution_root)
        command = _command(
            executor,
            role_name,
            escalation,
            escalation_execution_root,
            escalation_root,
            paths.claude / "agents",
            summary.get("thread_id") or state["thread_id"],
        )
        command, sandbox_exec = _contain_claude_write_command(
            command, escalation, escalation_execution_root, escalation_root, paths.home
        )
        _write_execution_record(
            escalation_root,
            state["role"],
            escalation,
            state["kind"],
            escalation_execution_root,
            config["parent_harness"],
        )
        atomic_write(escalation_root / "command.json", dump_json({
            "argv": command,
            "escalation": True,
            "previous_run": str(retry_root),
            "sandbox_exec": sandbox_exec,
        }))
        code = _invoke_safe(command, task, environment | {"CROSS_HARNESS_RUN_DIR": str(escalation_root)}, escalation_execution_root, escalation_root, escalation["timeout_seconds"])
        if escalation["harness"] == "claude":
            _write_claude_final_from_events(escalation_root)
        escalated_summary = finalize_run(
            escalation_root, state["role"], escalation, state["kind"], escalation_execution_root,
            code, new_state["attempts"] + 1, runtime_root=runtime_root,
        )
        escalated_state = json.loads((escalation_root / "state.json").read_text(encoding="utf-8"))
        escalated_state["escalated"] = True
        escalated_state["signatures"] = [*new_state["signatures"], *escalated_state.get("signatures", [])]
        atomic_write(escalation_root / "state.json", dump_json(escalated_state))
        return escalated_summary
    atomic_write(retry_root / "state.json", dump_json(new_state))
    return summary
